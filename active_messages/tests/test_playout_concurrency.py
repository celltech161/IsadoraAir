"""D2B native insertion and deck-open authority are serialized against
composition changes by the active-message POLICY lock (order policy ->
execution), with real concurrent PostgreSQL transactions:

* a composition change (clear, replacement, expiry, a limit change) that wins
  the policy lock first is seen: the old execution is never CLAIMED and no
  deck is authorized; a stale command is never admitted;
* a claim (or an admission) that wins first stands: a later change does not
  cut an already-claimed airing, and deck-open authority remains the final
  binding check for an admitted one;
* a lock wait beyond the engine's bound fails closed (no claim, no deck) and
  the existing not-aired report recovers; ordinary Tracks are unaffected;
* the locks are released when the claim commits -- never held through deck
  construction or the file open.

Each racing party runs in its own thread with its own database connection;
the test observes the waiter in pg_stat_activity before letting the holder
commit, so the interleaving is the one named, not a timing accident."""
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from active_messages import playout, services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastInsertion, ActiveBroadcastPolicy
from library.models import LogItem
from production.tests.support import ffmpeg_available

from .d2b_support import Playout, engine_stand_in

INSTANCE = eng_module.ENGINE_INSTANCE
POLICY_TABLE = ActiveBroadcastPolicy._meta.db_table
PATIENT = "30s"                      # a lock wait that outlasts any holder in these tests


class Party(threading.Thread):
    """Run ``body`` in its own thread/connection; re-raise its error on join."""

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

    def finish(self, timeout=60):
        self.join(timeout)
        if self.is_alive():
            raise AssertionError("party did not finish")
        if self.error is not None:
            raise self.error
        return self.result


def wait_for_policy_lock_waiter(timeout=20.0):
    """Block until some other backend is waiting for a lock on the policy row."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' AND query ILIKE %s",
                [f"%{POLICY_TABLE}%"])
            if cursor.fetchone()[0]:
                return
        time.sleep(0.005)
    raise AssertionError("nobody waited for the policy lock")


def hold(change, ready, release):
    """A composition-changing transaction that keeps the policy lock (and its
    uncommitted change) until released."""
    def body():
        with transaction.atomic():
            result = change()
            ready.set()
            if not release.wait(30):
                raise AssertionError("never released")
        return result
    return body


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ConcurrencyTests(Playout, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.log, (self.song_a, self.song_b) = self.playlist("Song A", "Song B")
        self.engine = engine_stand_in(self.log, [self.song_a, self.song_b], cursor=1)

    # -- helpers ----------------------------------------------------------------------------
    def inserted(self):
        execution, _publication, command = self.dispatched(actor=self.producer)
        self.engine._dispatch_engine_command(command)
        return execution, LogItem.objects.get(category__code="UrgentPA")

    def message_of(self, execution):
        return execution.composition_snapshot["components"][0]["message"]

    def stored(self, execution):
        return ActiveBroadcastExecution.objects.get(pk=execution.pk)

    def changes(self, execution):
        """Each composition-changing operation that serializes on the policy lock."""
        replacement = self.real_message(self.producer, kind="urgent", seconds=1.0)   # prepared outside the race
        message = self.message_of(execution)
        services.set_message_expiry(message, timezone.now() + timedelta(seconds=30), actor=self.admin)
        return {
            "clear": lambda: services.clear_message(message, actor=self.admin),
            "replacement": lambda: services.activate_message(replacement, actor=self.admin),
            "expiry": lambda: services.expire_due_messages(now=timezone.now() + timedelta(minutes=5)),
            "limits": lambda: services.set_limits(actor=self.admin, max_active_messages=10),
        }

    def change_wins_then(self, change, contender, *, patient=True):
        """``change`` takes the policy lock and changes the composition; then
        ``contender`` starts and is observed WAITING for the policy lock; only
        then does the change commit. Returns the contender's result."""
        ready, release = threading.Event(), threading.Event()
        changer = Party(hold(change, ready, release))
        changer.start()
        self.assertTrue(ready.wait(30))
        with mock.patch("active_messages.playout.AUTHORITY_LOCK_TIMEOUT", PATIENT if patient else "250ms"):
            racer = Party(contender)
            racer.start()
            wait_for_policy_lock_waiter()
            release.set()
            changer.finish()
            return racer.finish()

    def first_wins_then(self, first, change):
        """``first`` runs inside a transaction that keeps its locks; ``change``
        is observed WAITING for the policy lock; then ``first`` commits."""
        done, release = threading.Event(), threading.Event()
        holder = Party(hold(first, done, release))
        holder.start()
        self.assertTrue(done.wait(30))
        changer = Party(change)
        changer.start()
        wait_for_policy_lock_waiter()
        release.set()
        holder.finish()
        changer.finish()
        return holder

    # -- deck-open authority -------------------------------------------------------------------
    def test_a_composition_change_that_wins_the_policy_lock_is_never_claimed_or_opened(self):
        for label in ("clear", "replacement", "expiry", "limits"):
            with self.subTest(label):
                execution, native = self.inserted()
                change = self.changes(execution)[label]

                def deck_open():
                    # The engine's real deck-open scope: advisory playability
                    # reads the old committed generation and passes; the binding
                    # authority then waits on the policy lock.
                    with eng_module._deck_open_authority(native) as (track, reason):
                        return track, reason

                track, reason = self.change_wins_then(change, deck_open)
                self.assertIsNone(track, label)                                   # no deck authorized
                stored = self.stored(execution)
                self.assertNotEqual(stored.state, "claimed", label)
                self.assertEqual((stored.state, stored.failure_code), ("suppressed", "stale_generation"), label)
                self.assertIsNone(stored.claimed_at, label)
                self.reset_for_next_variant()

    def test_a_claim_that_wins_the_policy_lock_stands_through_a_later_clear(self):
        execution, native = self.inserted()
        message = self.message_of(execution)
        holder = self.first_wins_then(lambda: playout.authorize_open(native, INSTANCE),
                                      lambda: services.clear_message(message, actor=self.admin))
        self.assertTrue(holder.result.allowed)
        stored = self.stored(execution)
        self.assertEqual(stored.state, "claimed")                                # the clear did not cut it
        self.assertEqual(services.current_policy().generation, stored.policy_generation + 1)
        self.assertTrue(playout.authorize_open(native, INSTANCE).allowed)        # its deck stays authorized

    def test_a_claim_that_wins_stands_through_a_later_replacement(self):
        execution, native = self.inserted()
        replacement = self.real_message(self.producer, kind="urgent", seconds=1.0)
        self.first_wins_then(lambda: playout.authorize_open(native, INSTANCE),
                             lambda: services.activate_message(replacement, actor=self.admin))
        self.assertEqual(self.stored(execution).state, "claimed")

    # -- admission -----------------------------------------------------------------------------
    def test_a_stale_command_is_refused_by_admission_when_the_change_wins_first(self):
        execution, _publication, command = self.dispatched(actor=self.producer)
        message = self.message_of(execution)

        def admit_in_a_splice_transaction():
            with transaction.atomic():
                try:
                    playout.admit(command)
                except playout.Refused as refused:
                    return str(refused)
                return "admitted"

        outcome = self.change_wins_then(lambda: services.clear_message(message, actor=self.admin),
                                        admit_in_a_splice_transaction)
        self.assertIn(outcome, ("execution is suppressed", "stale generation"))   # it saw the committed change

    def test_a_stale_command_is_never_inserted_by_the_engine_when_the_change_wins_first(self):
        # The engine's own splice keeps its 250 ms bound: the command is either
        # refused after the change commits or times out -- never inserted.
        execution, _publication, command = self.dispatched(actor=self.producer)
        message = self.message_of(execution)
        with mock.patch.object(eng_module, "_close_old_connections", lambda: None), \
                mock.patch.object(eng_module.GLib, "timeout_add_seconds"):
            self.change_wins_then(lambda: services.clear_message(message, actor=self.admin),
                                  lambda: self.engine._insert_active_message(command))
        self.assertFalse(LogItem.objects.filter(category__code="UrgentPA").exists())
        self.assertFalse(ActiveBroadcastInsertion.objects.exists())
        self.assertEqual(len(self.engine.log_items), 2)                          # the queue untouched
        self.assertNotIn(self.stored(execution).state, ("accepted", "claimed"))

    def test_an_admission_that_wins_first_commits_and_deck_open_stays_the_final_check(self):
        execution, _publication, command = self.dispatched(actor=self.producer)
        message = self.message_of(execution)
        with mock.patch.object(eng_module, "_close_old_connections", lambda: None):
            self.first_wins_then(lambda: self.engine._insert_active_message_inner(command),
                                 lambda: services.clear_message(message, actor=self.admin))
        native = LogItem.objects.get(category__code="UrgentPA")
        self.assertEqual(self.stored(execution).state, "accepted")               # admitted before the clear
        with eng_module._deck_open_authority(native) as (track, _reason):
            self.assertIsNone(track)                                             # ...but never opened
        self.assertFalse(playout.authorize_open(native, INSTANCE).allowed)       # the binding authority
        stored = self.stored(execution)
        self.assertEqual((stored.state, stored.failure_code), ("suppressed", "stale_generation"))

    # -- contention fails closed ---------------------------------------------------------------
    def test_lock_contention_fails_closed_and_the_report_path_recovers(self):
        execution, native = self.inserted()
        ready, release = threading.Event(), threading.Event()
        holder = Party(hold(lambda: services._lock_policy(), ready, release))     # policy authority busy
        holder.start()
        self.assertTrue(ready.wait(30))
        try:
            with eng_module._deck_open_authority(native) as (track, reason):     # the engine's 250 ms bound
                self.assertIsNone(track)
                self.assertIn("authority unavailable", reason)
            with eng_module._deck_open_authority(self.song_b) as (track, reason):  # ordinary: no wait at all
                self.assertEqual((track.pk, reason), (self.song_b.track_id, None))
            self.assertEqual(self.stored(execution).state, "accepted")           # not claimed
            self.assertFalse(self.engine._report_active_message_left(native.pk, "deck_not_opened"))
            self.assertIn(native.pk, self.engine._active_message_held)           # held for the sweep
        finally:
            release.set()
            holder.finish()
        self.engine._active_message_report_retry_at = 0.0
        self.engine._queue_cursor = 2                                            # the deck path consumed it
        with self.committing():
            self.engine._sweep_active_message_queue()
        self.assertEqual(len(self.commands), 2)                                  # re-sent by the existing path
        self.assertFalse(playout.authorize_open(native, INSTANCE).allowed)       # the contended one is dead

    def test_admission_under_contention_inserts_nothing(self):
        _execution, _publication, command = self.dispatched(actor=self.producer)
        ready, release = threading.Event(), threading.Event()
        holder = Party(hold(lambda: services._lock_policy(), ready, release))
        holder.start()
        self.assertTrue(ready.wait(30))
        try:
            with mock.patch.object(eng_module.GLib, "timeout_add_seconds") as retry:
                self.engine._insert_active_message(command)                     # times out inside the splice
            retry.assert_called_once()                                           # the existing retry
            self.assertFalse(LogItem.objects.filter(category__code="UrgentPA").exists())
            self.assertEqual(len(self.engine.log_items), 2)
        finally:
            release.set()
            holder.finish()

    def test_advisory_checks_never_wait_on_an_uncommitted_composition_change(self):
        execution, native = self.inserted()
        message = self.message_of(execution)
        ready, release = threading.Event(), threading.Event()
        holder = Party(hold(lambda: services.clear_message(message, actor=self.admin), ready, release))
        holder.start()
        self.assertTrue(ready.wait(30))
        try:
            probe = Party(lambda: (eng_module._log_item_playable(native), playout.inspect(native, INSTANCE),
                                   playout.gate_open()))
            started = time.monotonic()
            probe.start()
            probe.join(5)
            self.assertFalse(probe.is_alive(), "the engine's advisory read waited on the policy change")
            (playable, _reason), decision, gate = probe.finish()
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertTrue(playable and decision.allowed and gate)              # the committed (old) state
        finally:
            release.set()
            holder.finish()

    # -- no lock is held through deck construction ----------------------------------------------
    def test_the_policy_lock_is_released_before_deck_construction(self):
        _execution, native = self.inserted()

        def try_lock():
            with transaction.atomic():
                ActiveBroadcastPolicy.objects.select_for_update(nowait=True).get(pk=services.POLICY_ID)
                return True

        with eng_module._deck_open_authority(native) as (track, _reason):
            self.assertIsNotNone(track)                                          # claimed...
            probe = Party(try_lock)                                              # ...and, while the deck is built,
            probe.start()
            self.assertTrue(probe.finish())                                      # nobody holds the policy

    # -- support --------------------------------------------------------------------------------
    def reset_for_next_variant(self):
        """Leave nothing unfinished so the next variant materializes afresh."""
        LogItem.objects.filter(category__code="UrgentPA").delete()
        self.engine = engine_stand_in(self.log, [self.song_a, self.song_b], cursor=1)
