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
from django.test import TransactionTestCase
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
        message = services.prepare_message(actor=self.admin, kind="urgent")
        candidate = media(self.admin)
        first, second = self.race(lambda: services.bind_message_media(message, candidate, actor=self.admin),
                                  lambda: retention.purge_media(candidate),
                                  "active_messages.services._lock_media")
        self.assertIsNone(first["error"])
        self.assertIsInstance(second["error"], PurgeRefused)
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=message.pk).message_media_id, candidate.pk)

    def test_a_binding_that_waits_for_a_purge_refuses_the_purged_media(self):
        message = services.prepare_message(actor=self.admin, kind="urgent")
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
