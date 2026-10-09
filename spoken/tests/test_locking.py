"""The transition service's row lock, with two real PostgreSQL sessions.

Session A takes the item's row lock inside a transition and pauses (a check
callback waits); session B's transition must BLOCK on that lock (proven from
pg_stat_activity), and once A commits, B re-reads the authoritative row and
refuses: a recalled item can no longer be submitted. Neither session can act
on a stale state.
"""
import threading
import time

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase

from spoken import transitions
from spoken.models import ItemState, SpokenItemEvent

from .test_domain import make_type, media

WAIT = 30
User = get_user_model()


class RowLockTests(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.manager = User.objects.create_user("manager", is_staff=True)
        self.item = transitions.create_item(make_type(), actor=self.owner)
        self.submission = media()

    def run_thread(self, name, target):
        box = {"error": None, "result": None}

        def run():
            try:
                box["result"] = target()
            except BaseException as exc:                    # noqa: BLE001 -- reported to the test
                box["error"] = exc
            finally:
                connections.close_all()
        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread, box

    def wait_until_blocked(self):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'")
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.02)
        self.fail("the second session never blocked on the spoken item row lock")

    def test_a_concurrent_transition_waits_for_the_lock_and_sees_the_committed_state(self):
        locked, release = threading.Event(), threading.Event()

        def pausing_check(row, user):
            locked.set()
            assert release.wait(WAIT)

        recaller, recall_box = self.run_thread("recaller", lambda: transitions._apply(
            self.item, ItemState.RECALLED, actor=self.manager, check=pausing_check))
        self.assertTrue(locked.wait(WAIT))
        submitter, submit_box = self.run_thread(
            "submitter", lambda: transitions.submit(self.item, self.submission, actor=self.owner))
        self.wait_until_blocked()
        self.assertTrue(submitter.is_alive())                  # really waiting, not done
        release.set()
        recaller.join(WAIT)
        submitter.join(WAIT)
        self.assertIsNone(recall_box["error"])
        self.assertIsInstance(submit_box["error"], transitions.InvalidTransition)
        self.item.refresh_from_db()
        self.assertEqual((self.item.state, self.item.submission_media_id), (ItemState.RECALLED, None))
        self.assertEqual(list(SpokenItemEvent.objects.filter(item=self.item).values_list("event", flat=True)),
                         ["created", "recalled"])
