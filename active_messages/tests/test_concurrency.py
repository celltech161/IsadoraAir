"""Races with real concurrent PostgreSQL sessions.

Each test holds one session inside a service transaction (paused right after
it took its locks) and proves the competing session BLOCKS on a lock (seen in
pg_stat_activity), then re-reads the committed state and does the right thing.

TransactionTestCase flushes every table after each test; serialized_rollback
restores the migrated seed data (the capability catalog) before each one.
"""
import threading
import time
import uuid
from datetime import timedelta
from unittest import mock

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from active_messages import services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage
from production.errors import PurgeRefused
from production.services import retention

from .support import Domain, media

WAIT = 30


class Sessions(Domain, TransactionTestCase):
    serialized_rollback = True

    def run_thread(self, target):
        box = {"error": None, "result": None}

        def runner():
            try:
                box["result"] = target()
            except BaseException as exc:                    # noqa: BLE001 -- reported to the test
                box["error"] = exc
            finally:
                connections.close_all()
        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        return thread, box

    def wait_until_blocked(self):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                               "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'")
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.02)
        self.fail("the competing session never blocked on a lock")

    def pause_after(self, target_path):
        """Patch ``target_path`` (called inside the service's transaction, after
        its locks) to signal and then wait for release."""
        reached, release = threading.Event(), threading.Event()
        original = _resolve(target_path)

        def pausing(*args, **kwargs):
            result = original(*args, **kwargs)
            if threading.current_thread() is not threading.main_thread() and not reached.is_set():
                reached.set()
                assert release.wait(WAIT)
            return result
        return mock.patch(target_path, pausing), reached, release

    def race(self, first, second, pause_target):
        patcher, reached, release = self.pause_after(pause_target)
        with patcher:
            one, one_box = self.run_thread(first)
            self.assertTrue(reached.wait(WAIT), "the first session never reached its pause point")
            two, two_box = self.run_thread(second)
            self.wait_until_blocked()
            self.assertTrue(two.is_alive())
            release.set()
            one.join(WAIT)
            two.join(WAIT)
        return one_box, two_box


def _resolve(path):
    module, name = path.rsplit(".", 1)
    import importlib
    return getattr(importlib.import_module(module), name)


class MaterializationRaceTests(Sessions):
    def test_two_workers_produce_one_due_occurrence(self):
        self.active()
        first, second = self.race(services.materialize_due_execution, services.materialize_due_execution,
                                  "active_messages.services._create_execution")
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        self.assertEqual(first["result"].pk, second["result"].pk)
        self.assertEqual(ActiveBroadcastExecution.objects.count(), 1)


class LifecycleRaceTests(Sessions):
    def test_concurrent_replacements_leave_exactly_one_active_message(self):
        a = self.prepared(self.producer, kind="urgent")
        b = self.prepared(self.producer, kind="urgent")
        first, second = self.race(lambda: services.activate_message(a, actor=self.admin),
                                  lambda: services.activate_message(b, actor=self.admin),
                                  "active_messages.services._suppress_stale")
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        states = dict(ActiveBroadcastMessage.objects.values_list("id", "state"))
        self.assertEqual((states[a.pk], states[b.pk]), ("cleared", "active"))
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=a.pk).superseded_by_id, b.pk)

    def test_activate_versus_clear_yields_one_deterministic_outcome(self):
        message = self.active(self.producer)
        replacement = self.prepared(self.producer)
        first, second = self.race(lambda: services.clear_message(message, actor=self.admin),
                                  lambda: services.activate_message(replacement, actor=self.admin),
                                  "active_messages.services._suppress_stale")
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        states = dict(ActiveBroadcastMessage.objects.values_list("id", "state"))
        self.assertEqual((states[message.pk], states[replacement.pk]), ("cleared", "active"))
        self.assertIsNone(ActiveBroadcastMessage.objects.get(pk=message.pk).superseded_by_id)   # cleared, not superseded

    def test_clear_racing_due_materialization_never_leaves_a_live_airing_of_the_cleared_message(self):
        message = self.active()
        first, second = self.race(services.materialize_due_execution,
                                  lambda: services.clear_message(message, actor=self.admin),
                                  "active_messages.services._create_execution")
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        execution = ActiveBroadcastExecution.objects.get()
        self.assertEqual((execution.state, execution.failure_code), ("suppressed", "stale_generation"))
        self.assertIsNone(services.materialize_due_execution())

    def test_expiry_racing_due_materialization(self):
        now = timezone.now()
        message = self.active(expires_at=now + timedelta(seconds=60), now=now)
        later = now + timedelta(seconds=61)
        first, second = self.race(lambda: services.materialize_due_execution(now=now),
                                  lambda: services.expire_due_messages(now=later),
                                  "active_messages.services._create_execution")
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=message.pk).state, "expired")
        self.assertEqual(ActiveBroadcastExecution.objects.get().state, "suppressed")


class BindingRaceTests(Sessions):
    def test_purge_waits_for_a_binding_and_then_refuses(self):
        message = services.prepare_message(actor=self.admin, content_type="urgent_pa")
        candidate = media(self.admin)
        first, second = self.race(lambda: services.bind_message_media(message, candidate, actor=self.admin),
                                  lambda: retention.purge_media(candidate),
                                  "active_messages.services._lock_media")
        self.assertIsNone(first["error"])
        self.assertIsInstance(second["error"], PurgeRefused)
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=message.pk).message_media_id, candidate.pk)

    def test_a_binding_that_waits_for_a_purge_refuses_the_purged_media(self):
        message = services.prepare_message(actor=self.admin, content_type="urgent_pa")
        candidate = media(self.admin)
        first, second = self.race(lambda: retention.purge_media(candidate),
                                  lambda: services.bind_message_media(message, candidate, actor=self.admin),
                                  "production.services.retention.find_references")
        self.assertIsNone(first["error"])
        self.assertIsInstance(second["error"], services.MediaRefused)
        self.assertEqual(second["error"].code, "media_purged")
        self.assertIsNone(ActiveBroadcastMessage.objects.get(pk=message.pk).message_media_id)


class ReplayTokenRaceTests(Sessions):
    def test_two_posts_of_one_replay_request_resolve_to_one_execution(self):
        self.active()
        token = uuid.uuid4()
        request = lambda: services.request_manual_replay(actor=self.admin, request_token=token)  # noqa: E731
        first, second = self.race(request, request, "active_messages.services._create_execution")
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        self.assertEqual(first["result"].pk, second["result"].pk)
        self.assertEqual(ActiveBroadcastExecution.objects.count(), 1)


class ReviewedPublicationRaceTests(Sessions):
    """D3D: an operator publishes exactly the draft revision they reviewed."""

    def test_a_save_waiting_on_a_reviewed_publication_cannot_change_the_published_audio(self):
        draft = self.prepared(self.producer)
        reviewed = ActiveBroadcastMessage.objects.get(pk=draft.pk).message_media_id
        revision = services.message_revision(draft)
        newer = media(self.producer)
        first, second = self.race(
            lambda: services.activate_message(draft, actor=self.admin, expected_revision=revision),
            lambda: services.bind_message_media(draft, newer, actor=self.producer),
            "active_messages.services._suppress_stale")
        self.assertIsNone(first["error"])
        self.assertIsInstance(second["error"], services.InvalidTransition)
        stored = ActiveBroadcastMessage.objects.get(pk=draft.pk)
        self.assertEqual((stored.state, stored.message_media_id), ("active", reviewed))

    def test_a_reviewed_publication_waiting_on_a_save_is_refused(self):
        draft = self.prepared(self.producer)
        revision = services.message_revision(draft)
        newer = media(self.producer)
        first, second = self.race(
            lambda: services.bind_message_media(draft, newer, actor=self.producer),
            lambda: services.activate_message(draft, actor=self.admin, expected_revision=revision),
            "active_messages.services._lock_media")
        self.assertIsNone(first["error"])
        self.assertIsInstance(second["error"], services.RevisionConflict)
        stored = ActiveBroadcastMessage.objects.get(pk=draft.pk)
        self.assertEqual((stored.state, stored.message_media_id), ("prepared", newer.pk))


@override_settings(SECURE_SSL_REDIRECT=False)
class ReplayOutcomeRaceTests(Sessions):
    """D3D: two simultaneous posts of one Replay Now token, through the board
    and two real database sessions -- one airing, and only one response says
    it created it."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import Group
        from library.models import GroupAccess
        for group in Group.objects.filter(name__startswith="test-"):
            GroupAccess.objects.update_or_create(group=group, defaults={"allowed_prefixes": "/iportal/"})
        patcher = mock.patch.object(services, "NATIVE_AIRING_AVAILABLE", True)   # controlled test conditions only
        patcher.start()
        self.addCleanup(patcher.stop)
        services.set_native_airing(True, actor=self.admin)

    def post_replay(self, token):
        from django.test import Client
        client = Client()
        client.force_login(self.controller)

        def request():
            response = client.post("/iportal/messages/board/replay/", {"request_token": token})
            page = client.get(response["Location"]).content.decode()
            return page.split('class="amb-flash"', 1)[1].split("</ul>", 1)[0]
        return request

    def test_simultaneous_posts_of_one_token_report_created_once(self):
        self.active()
        token = str(uuid.uuid4())
        first, second = self.race(self.post_replay(token), self.post_replay(token),
                                  "active_messages.services._create_execution")
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        execution = ActiveBroadcastExecution.objects.get()
        short = str(execution.pk)[:8]
        self.assertIn(f"new airing {short}", first["result"])
        self.assertIn(f"already requested from this page: airing {short}", second["result"])
        self.assertNotIn("new airing", second["result"])

    def test_a_simultaneous_different_token_is_reported_as_absorbed(self):
        self.active()
        first, second = self.race(self.post_replay(str(uuid.uuid4())), self.post_replay(str(uuid.uuid4())),
                                  "active_messages.services._create_execution")
        execution = ActiveBroadcastExecution.objects.get()
        self.assertIn(f"new airing {str(execution.pk)[:8]}", first["result"])
        self.assertIn(f"absorbed by the airing already in progress: {str(execution.pk)[:8]}", second["result"])


class BlockedExpiryRaceTests(Sessions):
    """D3E-A: two workers reach a permanently blocked composition just as one
    of its messages expires -- one sweep, one generation step, one new
    occurrence for the changed set, and no duplicate event."""

    def test_two_workers_expire_once_and_share_the_new_occurrence(self):
        from active_messages import worker
        from active_messages.models import ActiveBroadcastMessageEvent
        from production.services.render.engine import RenderError
        now = timezone.now()
        keep = self.active(kind="urgent", now=now)
        going = self.active(now=now)
        services.set_message_expiry(going, now + timedelta(seconds=60), actor=self.admin, now=now)
        failed = worker.run_once(now=now, render_fn=mock.Mock(side_effect=RenderError("render_failed", "x")))
        self.assertEqual(failed.result, "failed")
        self.assertEqual(worker.run_once(now=now, render_fn=mock.Mock()).result, "blocked")
        generation = services.current_policy().generation
        later = now + timedelta(minutes=2)

        def materialize():
            return services.materialize_due_execution(now=later, blocking_codes=worker.BLOCKING_CODES)
        first, second = self.race(materialize, materialize, "active_messages.services._expire_locked")
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        self.assertEqual(first["result"].pk, second["result"].pk)                 # coalesced
        self.assertEqual([c["message"] for c in first["result"].composition_snapshot["components"]], [str(keep.pk)])
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=going.pk).state, "expired")
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(event="expired").count(), 1)
        self.assertEqual(services.current_policy().generation, generation + 1)
        self.assertEqual(ActiveBroadcastExecution.objects.filter(state="pending_render").count(), 1)
