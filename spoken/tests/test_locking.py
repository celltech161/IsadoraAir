"""The transition service's row lock, with two real PostgreSQL sessions.

Session A takes the item's row lock inside a transition and pauses (a check
callback waits); session B's transition must BLOCK on that lock (proven from
pg_stat_activity), and once A commits, B re-reads the authoritative row and
refuses: a recalled item can no longer be submitted. Neither session can act
on a stale state.
"""
import threading
import time
from unittest import mock

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

        def pausing_check(row, user, now):
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


class LockedSnapshotConcurrencyTests(TransactionTestCase):
    """The complete-row expectation is the row as LOCKED by the transition. A
    concurrent ordinary edit (title/notes) cannot change the row until the
    transition commits -- it waits on the row lock -- and is then applied on
    top, touching only the editorial columns. No legitimate edit is refused
    and nothing is compared against a stale snapshot."""

    def test_an_ordinary_edit_waits_for_the_transition_then_lands(self):
        owner = User.objects.create_user("owner")
        item = transitions.submit(transitions.create_item(make_type(), actor=owner), media(), actor=owner)
        paused, release = threading.Event(), threading.Event()
        real_commit = transitions._commit

        def pausing_commit(row, plan):
            if threading.current_thread().name == "transition":
                paused.set()
                assert release.wait(WAIT)
            return real_commit(row, plan)

        def edit():
            row = transitions.SpokenItem.objects.get(pk=item.pk)
            row.title = "edited concurrently"
            row.save()
        with mock.patch.object(transitions, "_commit", pausing_commit):
            transition, transition_box = RowLockTests.run_thread(self, "transition",
                                                                 lambda: transitions.record_validation(item))
            self.assertTrue(paused.wait(WAIT))                     # the row is locked and snapshotted
            editor, editor_box = RowLockTests.run_thread(self, "editor", edit)
            RowLockTests.wait_until_blocked(self)                  # the edit waits on the row lock
            release.set()
            transition.join(WAIT)
            editor.join(WAIT)
        self.assertEqual((transition_box["error"], editor_box["error"]), (None, None))
        row = transitions.SpokenItem.objects.get(pk=item.pk)
        self.assertEqual((row.state, row.title), (ItemState.VALIDATED, "edited concurrently"))
