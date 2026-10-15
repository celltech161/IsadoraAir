"""D3C review corrections (2026-10-15).

A. CONTROL manages station Regular material of D3C-MANAGED types across
   producers -- withdraw/recall wherever the spoken lifecycle permits it --
   and gains nothing over unrelated standalone Phase-C types.
B. Authorization precedes every backend side effect: a refused request
   creates, links or synchronizes no backend and creates no item.
C. The ProducedContentType -> SpokenContentType link is bridge-only identity:
   no ordinary save or queryset write attaches, replaces or clears it, and a
   non-regular type never holds one (database CHECK too).
D. A TEST take always binds a type that is still TEST at the commit point
   (type row locked first; a concurrent mode change is serialized)."""
import threading
import time
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from library.models import Track
from produced_content import backend, no_air, regular
from produced_content.models import (
    BackendLinkRefused, ProducedContentTestTake, ProducedContentType, ProducedContentTypeAssignment,
    _grant_backend_link,
)
from production.services import render
from spoken import delivery, transitions
from spoken.models import AssetState, ItemState, SpokenAsset, SpokenContentType, SpokenItem
from spoken.tests.projection_support import ProjectionMixin, flac

from .support import PAGE, People, assign, media_row, no_air_type, regular_type

S = ItemState
PRODUCED_TABLE = ProducedContentType._meta.db_table
BACKEND_FIELDS = [field.attname for field in SpokenContentType._meta.concrete_fields]


def backend_snapshot():
    return sorted(tuple(row[name] for name in BACKEND_FIELDS)
                  for row in SpokenContentType.objects.values(*BACKEND_FIELDS))


def no_bridge(*args, **kwargs):
    raise AssertionError("a refused request must never reach the backend bridge")


@override_settings(SECURE_SSL_REDIRECT=False)
class Case(People, ProjectionMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.news = regular_type("town_news", output_mode="single", recipe_key="lcl_psa", name="Town News")
        self.psa = regular_type("local_psa", output_mode="accumulating", recipe_key="lcl_psa", name="Local PSA",
                                category_code="PCPSA")
        assign(self.producer, self.news, self.psa)
        assign(self.producer2, self.news, self.psa)

    def submitted(self, user, produced, title="t"):
        item = regular.start(user, produced, title=title)
        return regular.submit(user, item, media_row(user), expected_revision=regular.item_revision(item))

    def delivered(self, user, produced, title="t"):
        item = self.submitted(user, produced, title)
        item, token = transitions.start_processing(item, lease_seconds=600)
        rendition = self.stored_rendition(item, flac(next(self._lengths)))
        evidence = render.RenderEvidence(rendition_id=str(rendition.pk), source_id=str(rendition.derived_from_id),
                                         recipe_key=rendition.recipe_key, recipe_version=rendition.recipe_version,
                                         provenance_digest=rendition.recipe_params_digest)
        transitions.complete_processing(item, lease_token=token, evidence=evidence)
        delivery.deliver_item(item.pk)
        return SpokenItem.objects.get(pk=item.pk)

    def withdraw(self, user, item, expected=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.post(user, f"{PAGE}{item.pk}/withdraw/",
                             {"expected_state": expected or SpokenItem.objects.get(pk=item.pk).state})


class ControlAuthorityTests(Case):
    """A. Station control across producers -- managed types only."""

    def test_another_producer_still_cannot_see_or_withdraw_producer_material(self):
        draft = regular.start(self.producer, self.news, title="mine")
        delivered = self.delivered(self.producer, self.psa, "mine too")
        page = self.client_for(self.producer2).get(PAGE)
        for item in (draft, delivered):
            with self.subTest(state=item.state):
                self.assertNotContains(page, f'data-item="{item.pk}"')
                self.assertEqual(self.withdraw(self.producer2, item).status_code, 404)
                with self.assertRaises(regular.NotFound):
                    regular.withdraw(self.producer2, item.pk)
        with self.assertRaises(transitions.NotPermitted):                   # spoken itself refuses too
            transitions.recall(delivered.pk, actor=self.producer2)
        self.assertEqual({SpokenItem.objects.get(pk=draft.pk).state, SpokenItem.objects.get(pk=delivered.pk).state},
                         {S.DRAFT, S.DELIVERED})

    def test_control_lists_and_withdraws_another_producers_draft_and_validated_material(self):
        draft = regular.start(self.producer, self.news, title="draft")
        validated = self.submitted(self.producer, self.news, "validated")
        page = self.client_for(self.controller).get(PAGE)
        for item in (draft, validated):
            self.assertContains(page, f'data-item="{item.pk}"')
        self.assertContains(page, "by producer")
        confirm = self.client_for(self.controller).get(f"{PAGE}{draft.pk}/withdraw/")
        self.assertContains(confirm, "Abandon this item?")
        self.assertContains(confirm, "producer")
        for item in (draft, validated):
            with self.subTest(state=item.state):
                self.assertRedirects(self.withdraw(self.controller, item), PAGE, fetch_redirect_response=False)
                stored = SpokenItem.objects.get(pk=item.pk)
                self.assertEqual((stored.state, stored.recalled_by_id, stored.recall_reason),
                                 (S.RECALLED, self.controller.pk,
                                  f"{'abandon' if item is draft else 'withdraw'} by station control"))

    def test_processing_stays_non_interruptible_for_control(self):
        item = self.submitted(self.producer, self.news, "busy")
        transitions.start_processing(item.pk, lease_seconds=600)
        self.withdraw(self.controller, item, expected="processing")
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.PROCESSING)
        with self.assertRaises(regular.StateConflict):
            regular.withdraw(self.controller, item.pk)

    def test_control_recalls_another_producers_delivered_current_edition(self):
        item = self.delivered(self.producer, self.news, "today")
        asset = SpokenAsset.objects.get(current_item=item)
        self.assertTrue(Track.objects.get(pk=asset.track_id).ready2air)
        self.assertRedirects(self.withdraw(self.controller, item), PAGE, fetch_redirect_response=False)
        asset.refresh_from_db()
        self.assertEqual((asset.state, asset.current_item_id), (AssetState.PENDING, None))
        self.assertFalse(Track.objects.get(pk=asset.track_id).ready2air)
        self.assertIsNone(self.sha_on_disk(asset.library_relpath))
        stored = SpokenItem.objects.get(pk=item.pk)
        self.assertEqual((stored.state, stored.recalled_by_id), (S.RECALLED, self.controller.pk))

    def test_control_recalls_one_exact_accumulating_item(self):
        first = self.delivered(self.producer, self.psa, "first")
        second = self.delivered(self.producer2, self.psa, "second")
        self.withdraw(self.controller, first)
        gone, kept = SpokenAsset.objects.get(item=first), SpokenAsset.objects.get(item=second)
        self.assertEqual((gone.state, kept.state), (AssetState.RETIRED, AssetState.LIVE))
        self.assertFalse(Track.objects.get(pk=gone.track_id).ready2air)
        self.assertTrue(Track.objects.get(pk=kept.track_id).ready2air)
        self.assertEqual(SpokenItem.objects.get(pk=second.pk).state, S.DELIVERED)

    def test_a_stale_recall_of_a_superseded_edition_never_damages_the_newer_one(self):
        old = self.delivered(self.producer, self.news, "old")
        new = self.delivered(self.producer2, self.news, "new")
        self.assertEqual(SpokenItem.objects.get(pk=old.pk).state, S.SUPERSEDED)
        self.withdraw(self.controller, old)
        asset = SpokenAsset.objects.get(content_type__produced_content_type=self.news)
        self.assertEqual((asset.state, asset.current_item_id), (AssetState.LIVE, new.pk))
        self.assertTrue(Track.objects.get(pk=asset.track_id).ready2air)
        self.assertEqual(self.sha_on_disk(asset.library_relpath),
                         SpokenItem.objects.get(pk=new.pk).rendition_media.sha256)
        self.assertEqual(SpokenItem.objects.get(pk=old.pk).state, S.RECALLED)

    def test_standalone_phase_c_content_gains_no_control_authority(self):
        standalone = self.make_type(code="standalone", category="PCSTAND", recall_policy="owner_until_delivered")
        draft = transitions.create_item(standalone, actor=self.producer, title="standalone draft")
        live = self.processed(standalone, owner=self.producer)
        delivery.deliver_item(live.pk)
        for item in (draft, live):
            with self.subTest(state=SpokenItem.objects.get(pk=item.pk).state):
                with self.assertRaises(transitions.NotPermitted):
                    transitions.recall(item.pk, actor=self.controller)
                with self.assertRaises(regular.NotFound):
                    regular.withdraw(self.controller, item.pk)
                self.assertEqual(self.withdraw(self.controller, item).status_code, 404)
        self.assertNotContains(self.client_for(self.controller).get(PAGE), f'data-item="{draft.pk}"')
        self.assertEqual({SpokenItem.objects.get(pk=draft.pk).state, SpokenItem.objects.get(pk=live.pk).state},
                         {S.DRAFT, S.DELIVERED})
        self.assertTrue(transitions.may_recall_as_manager(self.manager, SpokenItem.objects.get(pk=live.pk)))  # staff

    def test_owner_recall_after_assignment_removal_is_unchanged(self):
        item = self.delivered(self.producer, self.psa, "kept")
        ProducedContentTypeAssignment.objects.filter(user=self.producer).delete()
        self.withdraw(self.producer, item)
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.RECALLED)


class AuthorizationBeforeSideEffectsTests(Case):
    """B. No refused request creates, links or synchronizes a backend."""

    def assert_untouched(self, produced, before_link, before_backends, before_items):
        self.assertEqual(ProducedContentType.objects.get(pk=produced.pk).spoken_content_type_id, before_link)
        self.assertEqual(backend_snapshot(), before_backends)
        self.assertEqual(SpokenItem.objects.count(), before_items)

    def refused_start(self, user, produced, error):
        state = (ProducedContentType.objects.get(pk=produced.pk).spoken_content_type_id, backend_snapshot(),
                 SpokenItem.objects.count())
        with mock.patch("produced_content.backend.ensure_regular_backend", side_effect=no_bridge), \
                self.assertRaises(error):
            regular.start(user, produced)
        self.assertEqual(self.post(user, f"{PAGE}start/", {"content_type": produced.key}).status_code, 302)
        self.assert_untouched(produced, *state)

    def test_an_unassigned_producer_creates_no_backend(self):
        self.refused_start(self.unassigned, self.news, regular.NotPermitted)
        self.assertEqual(SpokenContentType.objects.count(), 0)

    def test_a_disabled_type_creates_no_backend(self):
        self.news.enabled = False
        self.news.save()
        self.refused_start(self.producer, self.news, regular.NotPermitted)
        self.assertEqual(SpokenContentType.objects.count(), 0)

    def test_an_existing_backend_is_not_synchronized_by_a_refused_request(self):
        draft = regular.start(self.producer, self.news, title="before")
        backing = SpokenContentType.objects.get(pk=draft.content_type_id)
        backing.requires_review = True                                 # drift the bridge would repair
        backing.save()
        self.refused_start(self.unassigned, self.news, regular.NotPermitted)
        self.assertTrue(SpokenContentType.objects.get(pk=backing.pk).requires_review)
        produced = ProducedContentType.objects.get(pk=self.news.pk)
        produced.enabled = False
        produced.save()                                                 # no admin synchronize here
        self.refused_start(self.producer, produced, regular.NotPermitted)
        self.assertTrue(SpokenContentType.objects.get(pk=backing.pk).enabled)   # not even disabled by it

    def test_a_refused_submit_touches_nothing(self):
        draft = regular.start(self.producer, self.news, title="draft")
        backing = SpokenContentType.objects.get(pk=draft.content_type_id)
        backing.requires_review = True
        backing.save()
        before = (backing.pk, backend_snapshot(), SpokenItem.objects.count())
        ProducedContentTypeAssignment.objects.filter(user=self.producer, content_type=self.news).delete()
        for user in (self.producer, self.producer2, self.unassigned):
            with self.subTest(user=user.username), \
                    mock.patch("produced_content.backend.ensure_regular_backend", side_effect=no_bridge), \
                    self.assertRaises(regular.NotPermitted):
                regular.submit(user, draft, media_row(user), expected_revision=regular.item_revision(draft))
            self.assert_untouched(self.news, *before)
        self.assertEqual(SpokenItem.objects.get(pk=draft.pk).state, S.DRAFT)


class BridgeOnlyLinkTests(TestCase):
    """C. The link is identity: only the bridge first-links it."""

    def setUp(self):
        self.regular = regular_type("linked", recipe_key="lcl_psa")
        self.backing = backend.ensure_regular_backend(self.regular)
        self.other = SpokenContentType.objects.create(code="stray", name="Stray", delivery_mode="accumulating",
                                                      recipe_key="lcl_psa", target_category=self.backing.target_category)
        self.unlinked = regular_type("unlinked", recipe_key="lcl_psa", category_code="PCOTHER")
        self.test = no_air_type("practice")
        self.pa = ProducedContentType.objects.create(key="notice", name="notice", delivery_mode="public_address")
        self.urgent = ProducedContentType.objects.create(key="alert", name="alert", delivery_mode="urgent")

    def link_of(self, row):
        return ProducedContentType.objects.get(pk=row.pk).spoken_content_type_id

    def test_direct_attachment_is_refused_for_every_mode(self):
        for row in (self.unlinked, self.test, self.pa, self.urgent):
            with self.subTest(mode=row.delivery_mode):
                fresh = ProducedContentType.objects.get(pk=row.pk)
                fresh.spoken_content_type = self.other
                with self.assertRaises(BackendLinkRefused):
                    fresh.save(update_fields=["spoken_content_type"])
                with self.assertRaises(BackendLinkRefused):
                    fresh.save()
                with self.assertRaises(BackendLinkRefused):
                    ProducedContentType.objects.filter(pk=row.pk).update(spoken_content_type=self.other)
                with self.assertRaises(BackendLinkRefused):
                    ProducedContentType.objects.bulk_update([fresh], ["spoken_content_type"])
                self.assertIsNone(self.link_of(row))

    def test_replacing_or_clearing_a_linked_backend_is_refused(self):
        for value in (self.other, None):
            with self.subTest(value=value):
                fresh = ProducedContentType.objects.get(pk=self.regular.pk)
                fresh.spoken_content_type = value
                for kwargs in ({"update_fields": ["spoken_content_type"]}, {}):
                    with self.assertRaises(BackendLinkRefused):
                        fresh.save(**kwargs)
                with self.assertRaises(BackendLinkRefused):
                    ProducedContentType.objects.filter(pk=self.regular.pk).update(spoken_content_type=value)
                with self.assertRaises(BackendLinkRefused), _grant_backend_link(self.regular.pk, self.other.pk):
                    ProducedContentType.objects.filter(pk=self.regular.pk).update(spoken_content_type=self.other)
                self.assertEqual(self.link_of(self.regular), self.backing.pk)

    def test_a_grant_covers_one_exact_unlinked_regular_type_only(self):
        with self.assertRaises(BackendLinkRefused), _grant_backend_link(self.unlinked.pk, self.other.pk):
            ProducedContentType.objects.filter(pk=self.test.pk).update(spoken_content_type=self.other)
        with self.assertRaises(BackendLinkRefused), _grant_backend_link(self.test.pk, self.other.pk):
            ProducedContentType.objects.filter(pk=self.test.pk).update(spoken_content_type=self.other)
        with self.assertRaises(BackendLinkRefused), _grant_backend_link(self.unlinked.pk, self.other.pk):
            ProducedContentType.objects.filter(pk=self.unlinked.pk).update(spoken_content_type=self.other,
                                                                           name="smuggled")
        self.assertEqual([self.link_of(row) for row in (self.unlinked, self.test)], [None, None])

    def test_the_database_refuses_a_backend_on_a_non_regular_type(self):
        for row in (self.test, self.pa, self.urgent):
            with self.subTest(mode=row.delivery_mode), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"UPDATE {PRODUCED_TABLE} SET spoken_content_type_id = %s WHERE id = %s",
                                   [self.other.pk, row.pk])

    def test_the_bridge_still_links_and_stale_or_ordinary_saves_keep_the_link(self):
        produced = regular_type("fresh", recipe_key="lcl_psa", category_code="PCFRESH")
        stale = ProducedContentType.objects.get(pk=produced.pk)          # loaded before the first link
        linked = backend.ensure_regular_backend(produced)
        self.assertEqual(self.link_of(produced), linked.pk)
        stale.name = "Renamed by a stale form"
        stale.save()                                                     # ordinary fields still save
        current = ProducedContentType.objects.get(pk=produced.pk)
        self.assertEqual((current.name, current.spoken_content_type_id), ("Renamed by a stale form", linked.pk))
        current.description = "edited"
        current.save()
        current.refresh_from_db()
        current.sort_order = 3
        current.save(update_fields=["sort_order"])
        self.assertEqual(self.link_of(produced), linked.pk)

    def test_a_linked_type_cannot_leave_the_regular_mode(self):
        fresh = ProducedContentType.objects.get(pk=self.regular.pk)
        fresh.delivery_mode, fresh.target_category, fresh.output_mode, fresh.recipe_key = "test", None, "", ""
        with self.assertRaises(ValidationError):
            fresh.save()
        self.assertEqual(ProducedContentType.objects.get(pk=self.regular.pk).delivery_mode, "regular")


class _Party(threading.Thread):
    def __init__(self, body):
        super().__init__(daemon=True)
        self.body, self.result, self.error = body, None, None

    def run(self):
        try:
            self.result = self.body()
        except BaseException as exc:          # noqa: BLE001 -- surfaced by finish()
            self.error = exc
        finally:
            connection.close()

    def finish(self):
        self.join(60)
        if self.is_alive():
            raise AssertionError("party did not finish")
        return self.result, self.error


def _wait_for_type_lock_waiter(test, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                           "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' AND query ILIKE %s",
                           [f"%{PRODUCED_TABLE}%"])
            if cursor.fetchone()[0]:
                return
        time.sleep(0.005)
    test.fail("nobody waited for the content-type row lock")


class TestFirstSaveRaceTests(People, TransactionTestCase):
    """D. The first test save and a concurrent mode change, both orders, with
    real concurrent transactions (the waiter is observed in pg_stat_activity)."""

    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.practice = no_air_type("practice")
        assign(self.producer, self.practice)
        self.media = media_row(self.producer)

    def change_mode(self):
        row = ProducedContentType.objects.get(pk=self.practice.pk)
        row.delivery_mode = "public_address"
        row.save()

    def test_a_first_save_holding_the_type_lock_wins_and_the_mode_change_is_refused(self):
        ready, release = threading.Event(), threading.Event()

        def save_first():
            with transaction.atomic():
                take = no_air.save_take(self.producer, self.practice, self.media,
                                        expected_revision=no_air.NO_TAKE_REVISION)
                ready.set()
                release.wait(30)
            return take.pk

        holder = _Party(save_first)
        holder.start()
        self.assertTrue(ready.wait(30))
        changer = _Party(self.change_mode)
        changer.start()
        _wait_for_type_lock_waiter(self)                         # the mode change waits for the save
        release.set()
        take_pk, error = holder.finish()
        self.assertIsNone(error)
        _, error = changer.finish()
        self.assertIsInstance(error, ValidationError)
        self.assertIn("test takes", str(error))
        self.assertEqual(ProducedContentType.objects.get(pk=self.practice.pk).delivery_mode, "test")
        self.assertTrue(ProducedContentTestTake.objects.filter(pk=take_pk, content_type=self.practice).exists())

    def test_a_mode_change_holding_the_type_lock_wins_and_the_first_save_is_refused(self):
        ready, release = threading.Event(), threading.Event()

        def change_first():
            with transaction.atomic():
                self.change_mode()
                ready.set()
                release.wait(30)

        holder = _Party(change_first)
        holder.start()
        self.assertTrue(ready.wait(30))
        saver = _Party(lambda: no_air.save_take(self.producer, self.practice, self.media,
                                                expected_revision=no_air.NO_TAKE_REVISION))
        saver.start()
        _wait_for_type_lock_waiter(self)                         # the save waits for the mode change
        release.set()
        _, error = holder.finish()
        self.assertIsNone(error)
        _, error = saver.finish()
        self.assertIsInstance(error, no_air.NotFound)            # no longer a test type at its commit point
        self.assertEqual(ProducedContentType.objects.get(pk=self.practice.pk).delivery_mode, "public_address")
        self.assertFalse(ProducedContentTestTake.objects.exists())
