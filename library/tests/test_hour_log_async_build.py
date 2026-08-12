"""Regression coverage for the async hour-log build (engine.py commit
8711b6a, "move hour-log building off the main GLib thread") and the
follow-up queue-monotonicity/monitoring fix caught in code review
before that change went live.

Uses bare LogItem fixtures (no real Track/ScheduleBlock/Rotation data)
throughout -- these tests are about the surrounding async/locking/
threading infrastructure, not the rotation-picking algorithm itself,
which is exercised separately by normal usage and doesn't need
re-testing here.

Every class here uses TransactionTestCase, not plain TestCase. Several
tests spawn a REAL background thread (via _ensure_log_building, what a
production call to _ensure_upcoming_logs actually does) -- plain
TestCase wraps the whole test in one outer transaction on the main
thread's own connection, invisible to a genuinely separate connection
from another thread (default READ COMMITTED isolation can't see
another session's uncommitted rows), and in practice this project's
Django/psycopg2 combination surfaced that as "connection already
closed" errors that leaked across unrelated, later, even
non-threaded tests once a shared wrapped connection got closed
underneath Django's atomic-block bookkeeping. TransactionTestCase
commits for real and cleans up via TRUNCATE instead, which every
thread's own connection sees correctly -- worth the small, negligible
speed cost here (15 tests, ~1s) for not fighting that fragility.
Where a mock needs to create a PlaylistLog itself, it deletes any
existing row for that (date, hour) first, mirroring log_builder.
_persist_log's own delete-then-recreate pattern (PlaylistLog has a
unique_together on (date, hour))."""
import inspect
import threading
import time
from datetime import date
from unittest.mock import MagicMock, patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.models import Category, CategoryKind, LogItem, PlaylistLog
from library.services.log_builder import (
    LOCK_CONTENDED,
    build_and_approve_hour_log_locked,
    build_hour_log_for_admin,
)


def make_stand_in(running=True):
    """A bare PlaybackEngine instance, bypassing __init__ (no
    GStreamer/hardware setup needed) -- safe for methods that only
    touch instance attributes and the Django ORM."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.running = running
    obj.decks = {"A": None, "B": None}
    obj.manual_mode = False
    obj._lock = threading.RLock()
    obj._building_hours = set()
    obj.current_log = None
    obj.log_items = []
    obj._queue_cursor = 0
    obj._next_hour_peek = None
    obj._next_hour_peek_at = 0.0
    return obj


def fake_build_hour_log(target_date, target_hour, target_duration_seconds=None):
    """Stand-in for log_builder.build_hour_log that skips the real
    rotation-picking algorithm (and the Track/ScheduleBlock fixtures
    it would need) but mirrors _persist_log's delete-then-recreate
    behavior so repeated calls for the same (date, hour) don't hit the
    PlaylistLog unique_together constraint. Accepts (and ignores)
    target_duration_seconds -- build_and_approve_hour_log_locked always
    passes it through by keyword now."""
    PlaylistLog.objects.filter(date=target_date, hour=target_hour).delete()
    log = PlaylistLog.objects.create(date=target_date, hour=target_hour, status="draft")
    LogItem.objects.create(playlist_log=log, position=0, scheduled_time=timezone.now())
    return log, None


class HourLogFixtureMixin:
    """Plain setUp (not setUpTestData) -- works identically under both
    TestCase and TransactionTestCase, which doesn't support
    setUpTestData at all. The fixture here is trivial (two rows), so
    re-creating it per test instead of once per class costs nothing
    worth optimizing for."""

    def setUp(self):
        super().setUp()
        kind = CategoryKind.objects.create(code="test-kind", name="Test Kind")
        self.category = Category.objects.create(code="TESTCAT", name="Test Category", kind=kind)

    def make_log(self, d, hour, status="approved", item_count=1):
        log = PlaylistLog.objects.create(date=d, hour=hour, status=status)
        now = timezone.now()
        for i in range(item_count):
            LogItem.objects.create(
                playlist_log=log, position=i, scheduled_time=now, category=self.category,
            )
        return log


class QueueMonotonicityTests(HourLogFixtureMixin, TransactionTestCase):
    """A stale/older completed build must never move the active queue
    backward; forward progress -- including intentional early rollover
    -- must still work. Caught in review before this shipped: a
    next-hour worker finishing before a slower current-hour worker
    could otherwise have the later one overwrite the newer queue with
    stale content, since _advance_to_next_hour_log's only guard used
    to be exact equality, not ordering."""

    def test_rejects_older_completed_build(self):
        d = date(2027, 3, 1)
        older = self.make_log(d, 5)
        newer = self.make_log(d, 6)

        stand_in = make_stand_in()
        stand_in.current_log = newer
        stand_in.log_items = list(newer.items.all())

        eng_module.PlaybackEngine._advance_to_next_hour_log(stand_in, d, 5)

        self.assertEqual(stand_in.current_log.id, newer.id)

    def test_allows_forward_progress(self):
        d = date(2027, 3, 1)
        older = self.make_log(d, 5)
        newer = self.make_log(d, 6)

        stand_in = make_stand_in()
        stand_in.current_log = older
        stand_in.log_items = list(older.items.all())

        eng_module.PlaybackEngine._advance_to_next_hour_log(stand_in, d, 6)

        self.assertEqual(stand_in.current_log.id, newer.id)

    def test_install_built_hour_respects_the_same_guard(self):
        """The actual end-to-end path a completed async worker uses."""
        d = date(2027, 3, 1)
        older = self.make_log(d, 5)
        newer = self.make_log(d, 6)

        stand_in = make_stand_in()
        stand_in.current_log = newer
        stand_in.log_items = list(newer.items.all())
        stand_in._start_next_track = MagicMock()

        result = eng_module.PlaybackEngine._install_built_hour(stand_in, d, 5)

        self.assertFalse(result)
        self.assertEqual(stand_in.current_log.id, newer.id)


class EarlyRolloverMonitoringTests(HourLogFixtureMixin, TransactionTestCase):
    """Distinguishes an intentional early rollover (active log AHEAD of
    wall clock -- station policy: uninterrupted audio beats strict
    top-of-hour alignment, not a bug) from a genuine late/missed
    rollover (active log BEHIND wall clock) and an ordinary mid-hour
    cold start (no active log at all)."""

    def _run_with_fake_now(self, stand_in, fake_now, build_return):
        emitted = []
        # GLib is mocked too, not just the build function: when
        # build_return is a success, _build_hour_log_worker's success
        # path calls the REAL GLib.idle_add if left unmocked, which
        # registers a callback in the process-wide default GLib
        # context that never gets processed (no main loop runs in this
        # test process) -- but stays registered indefinitely,
        # referencing this test's stand-in and its since-rolled-back/
        # truncated DB rows, ready to fire against invalid state
        # (breaking a LATER, unrelated test's connection) the moment
        # anything anywhere in the process ever ticks a GLib main loop.
        with patch.object(eng_module.timezone, "localtime", return_value=fake_now), \
             patch.object(eng_module, "emit_event", side_effect=lambda *a, **kw: emitted.append(kw.get("title"))), \
             patch.object(eng_module, "build_and_approve_hour_log_locked", return_value=build_return), \
             patch.object(eng_module, "GLib"):
            eng_module.PlaybackEngine._ensure_upcoming_logs(stand_in)
            # _ensure_upcoming_logs unconditionally kicks off a
            # background build for (now.date(), now.hour) -- wait for
            # it to FULLY finish (including its own finally-block
            # cleanup) while the patches are still active. Exiting the
            # `with` block early would un-patch build_and_approve_hour_
            # log_locked out from under a still-running thread, which
            # would then call the REAL function against fixtures that
            # don't support it, and race the test database's teardown.
            deadline = time.time() + 5
            while stand_in._building_hours and time.time() < deadline:
                time.sleep(0.02)
        return emitted

    def test_no_warning_when_active_log_is_ahead_of_wall_clock(self):
        d = date(2027, 3, 1)
        ahead = self.make_log(d, 6)

        stand_in = make_stand_in()
        stand_in.current_log = ahead
        stand_in.log_items = list(ahead.items.all())
        stand_in._load_log_for = MagicMock()
        stand_in._start_next_track = MagicMock()

        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 5, 45))
        emitted = self._run_with_fake_now(stand_in, fake_now, (ahead, None))

        self.assertNotIn("Current hour's log not ready after rollover", emitted)
        self.assertNotIn("No approved log for current hour", emitted)

    def test_warning_when_active_log_is_genuinely_behind(self):
        d = date(2027, 3, 1)
        behind = self.make_log(d, 4)

        stand_in = make_stand_in()
        stand_in.current_log = behind
        stand_in.log_items = list(behind.items.all())
        stand_in._load_log_for = MagicMock()
        stand_in._start_next_track = MagicMock()

        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 5, 10))
        emitted = self._run_with_fake_now(stand_in, fake_now, (None, "no schedule block"))

        self.assertIn("Current hour's log not ready after rollover", emitted)

    def test_warning_when_no_active_log_at_all(self):
        stand_in = make_stand_in()
        stand_in.current_log = None
        stand_in._load_log_for = MagicMock()
        stand_in._start_next_track = MagicMock()

        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 5, 10))
        emitted = self._run_with_fake_now(stand_in, fake_now, (None, "no schedule block"))

        self.assertIn("No approved log for current hour", emitted)
        self.assertNotIn("Current hour's log not ready after rollover", emitted)


class AdvisoryLockTests(TransactionTestCase):
    """Approval must happen inside the SAME advisory-lock scope as
    persistence -- releasing before approval reopens the exact
    cross-process race this lock exists to close (caught in review:
    an earlier draft released the lock right after build_hour_log
    returned, leaving the approve step unprotected one line later)."""

    def test_second_call_returns_existing_approved_log_without_rebuilding(self):
        d = date(2027, 3, 2)
        hour = 6
        build_calls = []

        def counting_build(target_date, target_hour, target_duration_seconds=None):
            build_calls.append(1)
            return fake_build_hour_log(target_date, target_hour)

        with patch("library.services.log_builder.build_hour_log", side_effect=counting_build):
            log1, err1 = build_and_approve_hour_log_locked(d, hour)
            log2, err2 = build_and_approve_hour_log_locked(d, hour)

        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertEqual(len(build_calls), 1)
        self.assertEqual(log1.id, log2.id)
        self.assertEqual(log1.status, "approved")

    def test_concurrent_builds_produce_one_success_and_one_contended(self):
        d = date(2027, 3, 2)
        hour = 7
        key1, key2 = d.toordinal(), hour

        release_event = threading.Event()
        holder_ready = threading.Event()

        def hold_lock():
            close_old_connections()
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [key1, key2])
                got = cursor.fetchone()[0]
            holder_ready.set()
            if got:
                release_event.wait(timeout=5)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [key1, key2])
            connection.close()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        holder_ready.wait(timeout=5)

        try:
            log, error = build_and_approve_hour_log_locked(d, hour)
        finally:
            release_event.set()
            holder.join(timeout=5)

        self.assertIsNone(log)
        self.assertEqual(error, LOCK_CONTENDED)


class AdminRebuildSemanticsTests(TransactionTestCase):
    """api_log_build's rebuild-as-draft behavior must survive
    unchanged -- routing it through the engine's auto-approving,
    skip-if-already-approved wrapper instead of its own would silently
    turn the admin rebuild button into a no-op whenever an approved log
    already existed for that hour."""

    def test_admin_rebuild_always_rebuilds_and_never_auto_approves(self):
        d = date(2027, 3, 3)
        hour = 5

        with patch("library.services.log_builder.build_hour_log", side_effect=fake_build_hour_log):
            approved_log, approve_error = build_and_approve_hour_log_locked(d, hour)
            self.assertIsNone(approve_error)
            self.assertEqual(approved_log.status, "approved")

            rebuilt_log, rebuild_error = build_hour_log_for_admin(d, hour)

        self.assertIsNone(rebuild_error)
        self.assertNotEqual(rebuilt_log.id, approved_log.id, "admin rebuild must not silently no-op when an approved log exists")
        self.assertEqual(rebuilt_log.status, "draft")


class NeverSynchronousTests(TransactionTestCase):
    """_ensure_upcoming_logs must never call the builder on its own
    (the main GLib) thread, under any condition -- deck-idle state
    alone doesn't prove the engine has no live audio (studio mic,
    Remote DJ, FX/VT) that a synchronous ~3s build would still block."""

    def test_ensure_upcoming_logs_never_calls_the_builder_on_the_main_thread(self):
        d = date(2027, 3, 4)
        hour = 8

        stand_in = make_stand_in()
        stand_in._load_log_for = MagicMock()
        stand_in._start_next_track = MagicMock()

        main_thread_calls = []

        def spy_build(target_date, target_hour, target_duration_seconds=None):
            main_thread_calls.append(threading.current_thread() is threading.main_thread())
            return (None, "no schedule block")

        fake_now = timezone.make_aware(timezone.datetime(d.year, d.month, d.day, hour, 15))
        with patch.object(eng_module.timezone, "localtime", return_value=fake_now), \
             patch.object(eng_module, "emit_event"), \
             patch.object(eng_module, "build_and_approve_hour_log_locked", side_effect=spy_build):
            eng_module.PlaybackEngine._ensure_upcoming_logs(stand_in)
            # Wait for the spy to have recorded AND for the worker's own
            # finally-block cleanup to finish, all while the patches are
            # still active (see EarlyRolloverMonitoringTests._run_with_
            # fake_now for why exiting early is unsafe).
            deadline = time.time() + 5
            while (not main_thread_calls or stand_in._building_hours) and time.time() < deadline:
                time.sleep(0.02)

        self.assertEqual(main_thread_calls, [False])

    def test_no_direct_call_to_the_builder_in_source(self):
        """Static confirmation, independent of timing: the function
        body itself contains no direct call to the build function, only
        to the async dispatcher."""
        src = inspect.getsource(eng_module.PlaybackEngine._ensure_upcoming_logs)
        self.assertNotIn("build_and_approve_hour_log_locked(", src)
        self.assertIn("_ensure_log_building(", src)


class ShutdownGuardTests(TransactionTestCase):
    """A completed daemon worker must never schedule (or act on) a
    queue mutation after the engine has started tearing down."""

    def test_worker_does_not_schedule_install_when_running_is_false(self):
        d = date(2027, 3, 4)
        hour = 9
        stand_in = make_stand_in(running=False)

        with patch.object(eng_module, "build_and_approve_hour_log_locked", return_value=(None, "no schedule block")), \
             patch.object(eng_module, "GLib") as mock_glib:
            eng_module.PlaybackEngine._build_hour_log_worker(stand_in, d, hour)

        self.assertFalse(mock_glib.idle_add.called)

    def test_install_built_hour_bails_immediately_when_running_is_false(self):
        d = date(2027, 3, 4)
        hour = 10
        stand_in = make_stand_in(running=False)

        result = eng_module.PlaybackEngine._install_built_hour(stand_in, d, hour)

        self.assertFalse(result)
        self.assertIsNone(stand_in.current_log)


class ColdStartPlaybackTests(HourLogFixtureMixin, TransactionTestCase):
    """_advance_to_next_hour_log never itself starts playback -- a
    genuine cold start needs _install_built_hour to also perform the
    idle-recovery check, or a fully built-and-approved log would sit
    silently unplayed until the next 10s tick noticed."""

    def test_install_built_hour_starts_playback_when_decks_are_empty(self):
        d = date(2027, 3, 5)
        hour = 5
        log = self.make_log(d, hour)

        stand_in = make_stand_in()
        stand_in._start_next_track = MagicMock()

        result = eng_module.PlaybackEngine._install_built_hour(stand_in, d, hour)

        self.assertFalse(result)
        self.assertEqual(stand_in.current_log.id, log.id)
        stand_in._start_next_track.assert_called_once()

    def test_install_built_hour_does_not_start_playback_when_a_deck_is_occupied(self):
        d = date(2027, 3, 5)
        hour = 6
        self.make_log(d, hour)

        stand_in = make_stand_in()
        stand_in.decks = {"A": object(), "B": None}
        stand_in._start_next_track = MagicMock()

        eng_module.PlaybackEngine._install_built_hour(stand_in, d, hour)

        stand_in._start_next_track.assert_not_called()
