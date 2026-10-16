"""iPortal D3C TEST / NO AIR: the signed-in producer's own saved test take of
one exact test type -- validated, temporary ProductionMedia only, with an
optimistic revision, seven-day retention after the most recent save, safe
purge of unbound media -- and structurally NOTHING that could air: no
SpokenItem, rendition, SpokenAsset, Track, LogItem, active message or
execution, no render, no engine command."""
import contextlib
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage
from library.models import LogItem, Track
from produced_content import no_air
from produced_content.models import ProducedContentTestTake, ProducedContentType, ProducedContentTypeAssignment
from production.models import ProductionMedia
from spoken.management.commands import spoken_render_worker
from spoken.models import SpokenAsset, SpokenAssetEdition, SpokenItem

from .support import API, PAGE, RECORDER, People, assign, media_row, no_air_type, regular_type

PURGED = ProductionMedia.RETENTION_PURGED


def forbidden(*args, **kwargs):
    raise AssertionError("TEST / NO AIR must never reach this")


@contextlib.contextmanager
def nothing_airable():
    """Every path that could make audio airable fails the test if reached."""
    with mock.patch("production.services.render.render", side_effect=forbidden), \
            mock.patch("spoken.transitions.create_item", side_effect=forbidden), \
            mock.patch("spoken.transitions.submit", side_effect=forbidden), \
            mock.patch("spoken.delivery.deliver_item", side_effect=forbidden), \
            mock.patch("isadoraair.engine_commands.enqueue_engine_command", side_effect=forbidden), \
            mock.patch("active_messages.services.prepare_message", side_effect=forbidden):
        yield


@override_settings(SECURE_SSL_REDIRECT=False)
class NoAirCase(People, TestCase):
    def setUp(self):
        super().setUp()
        self.practice = no_air_type("practice", name="Practice")
        self.other = no_air_type("drills", name="Drills")
        assign(self.producer, self.practice)
        assign(self.producer2, self.practice)
        self.query = "test_type=practice"

    def ctx(self, user=None, query=None):
        response = self.context(user or self.producer, query or self.query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["context"]

    def save(self, user, media_id, revision, query=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.commit(user, {"test_type": (query or self.query).split("=", 1)[1]}, media_id, revision)

    def saved(self, user=None):
        user = user or self.producer
        take = self.recorded(user, self.query)
        response = self.save(user, take, self.ctx(user)["revision"])
        self.assertEqual(response.status_code, 200, response.content)
        return take

    def assert_nothing_airable_exists(self):
        self.assertEqual(
            {model.__name__: model.objects.count() for model in (SpokenItem, SpokenAsset, SpokenAssetEdition, Track,
                                                                 LogItem, ActiveBroadcastMessage,
                                                                 ActiveBroadcastExecution)},
            dict.fromkeys(("SpokenItem", "SpokenAsset", "SpokenAssetEdition", "Track", "LogItem",
                           "ActiveBroadcastMessage", "ActiveBroadcastExecution"), 0))
        self.assertFalse(ProductionMedia.objects.filter(kind=ProductionMedia.KIND_RENDITION).exists())


class RecorderTests(NoAirCase):
    def test_the_workspace_says_test_no_air_and_saves_a_test_take(self):
        page = self.client_for(self.producer).get(f"{RECORDER}?{self.query}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "TEST / NO AIR")
        context = self.ctx()
        self.assertEqual(context["subject"], {"test_type": "practice"})
        self.assertEqual((context["terms"]["badge"], context["terms"]["save"]), ("TEST / NO AIR", "Save test take"))
        self.assertIn("Nothing in this workspace can air.", context["purpose"])
        self.assertIn("nothing in this workspace can air", context["terms"]["editor_hint"])
        self.assertEqual(set(context["allowed_operations"]), {"open", "export", "record", "import", "save"})
        self.assertEqual((context["revision"], context["current"]), (no_air.NO_TAKE_REVISION, None))

    def test_record_import_edit_save_preview_replace_and_remove_create_nothing_airable(self):
        with nothing_airable():
            recorded = self.saved()                                                   # record + save
            take = ProducedContentTestTake.objects.get()
            self.assertEqual((take.user_id, take.content_type_id, str(take.media_id), take.revision),
                             (self.producer.pk, self.practice.pk, recorded, 1))
            self.assertAlmostEqual((take.expires_at - timezone.now()).total_seconds(), 7 * 86400, delta=60)
            context = self.ctx()
            self.assertEqual(context["current"]["media_id"], recorded)
            self.assertIn("edit", context["allowed_operations"])
            self.assertIn("remove", context["allowed_operations"])
            preview = self.client_for(self.producer).get(context["current"]["preview_url"])     # preview
            self.assertEqual(preview.status_code, 200)
            source = self.client_for(self.producer).get(f"{API}source/?{self.query}")          # editor source
            self.assertEqual(source.status_code, 200)
            edited = self.recorded(self.producer, self.query, mode="edit",                     # edit
                                   extra=f"&derived_from={recorded}&operations=%5B%22trim%22%5D")
            response = self.save(self.producer, edited, context["revision"])                   # replace
            self.assertEqual(response.status_code, 200, response.content)
            take.refresh_from_db()
            self.assertEqual((str(take.media_id), take.revision), (edited, 2))
            # The recording the edit derives from stays referenced (derived_from): its purge is refused,
            # harmlessly; ordinary retention decides about it.
            self.assertNotEqual(ProductionMedia.objects.get(pk=recorded).retention_state, PURGED)
            imported = self.recorded(self.producer, self.query, mode="import")                 # import
            self.assertEqual(self.save(self.producer, imported, self.ctx()["revision"]).status_code, 200)
            self.assertEqual(ProductionMedia.objects.get(pk=edited).retention_state, PURGED)    # unbound -> purged
            response = self.post_json(self.producer, f"{API}remove/", {"subject": {"test_type": "practice"},
                                                                       "revision": self.ctx()["revision"]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(ProducedContentTestTake.objects.exists())
        self.assert_nothing_airable_exists()

    def test_a_stale_tab_gets_a_conflict_never_a_silent_overwrite(self):
        revision = self.ctx()["revision"]                                    # two tabs, no take yet
        first = self.recorded(self.producer, self.query)
        second = self.recorded(self.producer, self.query)
        self.assertEqual(self.save(self.producer, first, revision).status_code, 200)
        response = self.save(self.producer, second, revision)
        self.assertEqual((response.status_code, response.json()["error"]), (409, "stale_revision"))
        current = self.ctx()["revision"]
        third = self.recorded(self.producer, self.query)
        self.assertEqual(self.save(self.producer, third, current).status_code, 200)
        self.assertEqual(self.save(self.producer, second, current).status_code, 409)       # stale again
        self.assertEqual(str(ProducedContentTestTake.objects.get().media_id), third)

    def test_the_binding_is_per_signed_in_user(self):
        mine = self.saved(self.producer)
        theirs_context = self.ctx(self.producer2)
        self.assertIsNone(theirs_context["current"])                         # producer2 has their own (empty) one
        for path in (f"{API}media/{mine}/?{self.query}", f"{API}media/{mine}/?{self.query}&download=1"):
            self.assertEqual(self.client_for(self.producer2).get(path).status_code, 404)
        self.assertEqual(self.save(self.producer2, mine, theirs_context["revision"]).status_code, 404)
        theirs = self.saved(self.producer2)
        self.assertEqual({(take.user_id, str(take.media_id)) for take in ProducedContentTestTake.objects.all()},
                         {(self.producer.pk, mine), (self.producer2.pk, theirs)})

    def test_unassigned_users_and_other_modes_do_not_resolve(self):
        self.assertEqual(self.context(self.unassigned, self.query).status_code, 404)
        self.assertEqual(self.context(self.producer, "test_type=drills").status_code, 404)     # not assigned
        regular_type("news_x")
        self.assertEqual(self.context(self.producer, "test_type=news_x").status_code, 404)     # not a test type
        self.assertEqual(self.context(self.host, self.query).status_code, 403)
        self.assertEqual(self.ctx(self.controller, "test_type=drills")["subject"], {"test_type": "drills"})

    def test_losing_the_assignment_leaves_the_take_viewable_and_removable_but_not_writable(self):
        self.saved()
        ProducedContentTypeAssignment.objects.filter(user=self.producer).delete()
        context = self.ctx()
        self.assertNotIn("save", context["allowed_operations"])
        self.assertIn("remove", context["allowed_operations"])
        self.assertEqual(self.take(self.producer, self.query).status_code, 403)
        response = self.post_json(self.producer, f"{API}remove/", {"subject": {"test_type": "practice"},
                                                                   "revision": context["revision"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.context(self.producer, self.query).status_code, 404)

    def test_my_content_shows_the_saved_take(self):
        self.saved()
        page = self.client_for(self.producer).get(PAGE)
        self.assertContains(page, 'data-content-type="practice"')
        self.assertContains(page, "data-test-take")
        self.assertContains(page, "Nothing in this workspace can air.")


class RetentionTests(NoAirCase):
    def test_takes_expire_seven_days_after_the_most_recent_save_and_their_media_is_purged(self):
        recorded = self.saved()
        take = ProducedContentTestTake.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(no_air.expire_due(now=timezone.now() + timedelta(days=6)), 0)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(no_air.expire_due(now=take.expires_at), 1)
        self.assertFalse(ProducedContentTestTake.objects.exists())
        self.assertEqual(ProductionMedia.objects.get(pk=recorded).retention_state, PURGED)
        self.assert_nothing_airable_exists()

    def test_a_new_save_restarts_the_retention_clock(self):
        self.saved()
        take = ProducedContentTestTake.objects.get()
        first_expiry = take.expires_at
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(days=5)):
            self.saved()
        take.refresh_from_db()
        self.assertGreater(take.expires_at, first_expiry + timedelta(days=4))
        self.assertEqual(no_air.expire_due(now=first_expiry + timedelta(minutes=1)), 0)

    def test_a_purge_refusal_is_harmless(self):
        recorded = self.saved()
        child = media_row(self.producer, derived_from=ProductionMedia.objects.get(pk=recorded),
                          kind=ProductionMedia.KIND_EDIT, airing_prohibited=True)  # something else references it
        take = ProducedContentTestTake.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(no_air.expire_due(now=take.expires_at), 1)
        self.assertFalse(ProducedContentTestTake.objects.exists())
        self.assertNotEqual(ProductionMedia.objects.get(pk=recorded).retention_state, PURGED)
        self.assertTrue(ProductionMedia.objects.filter(pk=child.pk).exists())

    def test_the_sweep_is_bounded(self):
        for user in (self.producer, self.producer2):
            self.saved(user)
        later = timezone.now() + timedelta(days=8)
        self.assertEqual(no_air.expire_due(limit=1, now=later), 1)
        self.assertEqual(no_air.expire_due(limit=1, now=later), 1)
        self.assertEqual(no_air.expire_due(limit=1, now=later), 0)

    def test_the_spoken_worker_loop_runs_the_sweep_throttled_and_failure_contained(self):
        state, clock = {}, iter([100.0, 110.0, 161.0, 300.0])
        with mock.patch("produced_content.no_air.expire_due", return_value=2) as sweep:
            self.assertEqual(spoken_render_worker.sweep_test_takes(state, clock=lambda: next(clock)), 2)
            self.assertEqual(spoken_render_worker.sweep_test_takes(state, clock=lambda: next(clock)), 0)
            self.assertEqual(spoken_render_worker.sweep_test_takes(state, clock=lambda: next(clock)), 2)
        self.assertEqual(sweep.call_count, 2)
        with mock.patch("produced_content.no_air.expire_due", side_effect=RuntimeError("database hiccup")), \
                self.assertLogs("spoken.management.commands.spoken_render_worker", "ERROR"):
            self.assertEqual(spoken_render_worker.sweep_test_takes(state, clock=lambda: next(clock)), 0)


class ModelTests(NoAirCase):
    def test_one_binding_per_user_and_type_and_the_type_mode_is_then_fixed(self):
        self.saved()
        practice = ProducedContentType.objects.get(pk=self.practice.pk)
        practice.delivery_mode = "public_address"
        with self.assertRaises(Exception):
            practice.save()
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProducedContentTestTake.objects.create(user=self.producer, content_type=self.practice,
                                                   media=media_row(self.producer), expires_at=timezone.now())

    def test_a_disabled_test_type_refuses_new_saves(self):
        self.practice.enabled = False
        self.practice.save()
        self.assertEqual(self.context(self.producer, self.query).status_code, 404)
        with self.assertRaises(no_air.NotPermitted):
            no_air.save_take(self.producer, self.practice, media_row(self.producer),
                             expected_revision=no_air.NO_TAKE_REVISION)
