"""Regression coverage for the song-request scheduling lifecycle
(pending/no_slot_soon -> scheduled -> fulfilled) and hour-rollover
reconciliation.

Background: a request used to be marked "fulfilled" the instant it was
swapped into a LogItem, which could be minutes before the track
actually aired -- or, if an hour-boundary rollover discarded that
LogItem first, never at all, leaving the request stuck showing
"fulfilled" forever. This suite covers the fix: a distinct "scheduled"
state for "assigned, not yet aired," promoted to "fulfilled" only from
the engine's real air-start event, plus reconciliation that requeues a
request whose assignment gets stranded, and the concurrency-safety
fixes that came out of review (returning the locked object rather than
mutating the caller's stale one, bounded-wait lock contention returning
a sentinel rather than stale data, capacity counted per distinct
LogItem across both scheduled+fulfilled, sequential idempotence against
an already-reserved slot).

TransactionTestCase throughout, following the pattern established in
library/tests/test_hour_log_async_build.py -- several tests here spawn
real threads that hold row locks or race against the scheduler, which
plain TestCase's shared outer transaction can't see correctly across
separate connections."""
import importlib
import inspect
import json
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib import admin
from django.core.management import call_command
from django.db import close_old_connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Track
from webrequests.admin import SongRequestAdmin
from webrequests.management.commands.refresh_song_request_statuses import Command as RefreshCommand
from webrequests.models import SongRequest, WebRequestConfig
from webrequests.services import (
    SCHEDULING_CONTENDED,
    classify_log_item,
    maybe_schedule_song_request,
    mark_song_requests_aired,
    track_is_available,
)
import webrequests.management.commands.refresh_song_request_statuses as refresh_module
import webrequests.services as services_module

_migration_0006 = importlib.import_module("webrequests.migrations.0006_scheduled_status")


class _FakeApps:
    """Stand-in for the `apps` argument RunPython migration functions
    receive -- just needs get_model to return the real model, since
    these tests exercise the migration function directly against real
    fixtures rather than replaying migration history."""

    def get_model(self, app_label, model_name):
        return SongRequest


def make_engine_stand_in():
    """A bare PlaybackEngine instance, bypassing __init__ -- same
    technique as library/tests/test_hour_log_async_build.py's
    make_stand_in, safe for methods that only touch instance attributes
    and the Django ORM (no GStreamer/hardware setup needed)."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.running = True
    obj.decks = {"A": None, "B": None}
    obj.manual_mode = False
    obj._lock = threading.RLock()
    obj._next_triggered = False
    obj._deck_bin_map = {}
    return obj


class WebRequestFixtureMixin:
    """Plain setUp (not setUpTestData) -- required for TransactionTestCase
    compatibility. Fake engine_state.json lives in a real temp file so
    track_is_available's filesystem check (and any other Path-based
    check) has something real to look at; track fixtures similarly get
    real (empty) files on disk rather than fake paths, since
    track_is_available/is_track_eligible_at both require the file to
    actually exist."""

    def setUp(self):
        super().setUp()
        # "music" is seeded by library/migrations/0017_seed_category_kinds.py
        # and survives into the test DB before the first test's teardown
        # flushes it -- get_or_create so this fixture works whether or
        # not that seeded row is still present for this particular test.
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.category = Category.objects.create(code="TESTMUS", name="Test Music", kind=kind)
        self.artist = Artist.objects.create(name="Test Artist")

        WebRequestConfig.objects.all().delete()
        self.cfg = WebRequestConfig.objects.create(
            enabled=True, open_slots=list(range(168)), max_fulfilled_per_hour=4,
            lookahead_warning_minutes=60, expire_after_hours=6,
        )

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.state_path = Path(self._tmpdir.name) / "engine_state.json"
        patcher = patch.object(services_module, "ENGINE_STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self._track_counter = 0

    def make_track(self, title="Test Song", ready2air=True, filepath=None):
        self._track_counter += 1
        real_path = Path(self._tmpdir.name) / f"track-{self._track_counter}.mp3"
        real_path.touch()
        return Track.objects.create(
            title=title, artist=self.artist, category=self.category,
            ready2air=ready2air, filepath=filepath or str(real_path),
            duration_seconds=180.0,
        )

    def make_log(self, d, hour, status="approved"):
        PlaylistLog.objects.filter(date=d, hour=hour).delete()
        return PlaylistLog.objects.create(date=d, hour=hour, status=status)

    def make_item(self, log, position, scheduled_time=None, track=None, played_at=None):
        return LogItem.objects.create(
            playlist_log=log, position=position,
            scheduled_time=scheduled_time or timezone.now(),
            track=track, category=self.category, played_at=played_at,
        )

    def make_request(self, track, status="pending", submitted_at=None, **extra):
        return SongRequest.objects.create(
            external_request_id=f"ext-{SongRequest.objects.count() + 1}-{track.id}",
            track=track, status=status,
            submitted_at=submitted_at or timezone.now(),
            **extra,
        )

    def write_state(self, decks=None, queue=None, date_str=None, hour=None, timestamp=None):
        state = {
            "decks": decks if decks is not None else {"A": None, "B": None},
            "queue": queue or [],
            "date": date_str,
            "hour": hour,
            "timestamp": timestamp if timestamp is not None else time.time(),
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")


class TrackAvailabilityAndClassificationTests(WebRequestFixtureMixin, TransactionTestCase):
    def test_track_is_available_true_for_good_track(self):
        self.assertTrue(track_is_available(self.make_track()))

    def test_track_is_available_false_when_file_missing(self):
        missing = str(Path(self._tmpdir.name) / "does-not-exist.mp3")
        self.assertFalse(track_is_available(self.make_track(filepath=missing)))

    def test_track_is_available_false_when_ready2air_off(self):
        self.assertFalse(track_is_available(self.make_track(ready2air=False)))

    def test_track_is_available_false_for_none(self):
        self.assertFalse(track_is_available(None))

    def test_classify_none_log_item_is_stranded_regardless_of_state(self):
        self.assertEqual(classify_log_item(None, None), "STRANDED")
        self.assertEqual(classify_log_item(None, {"date": "2027-01-01", "hour": 5}), "STRANDED")

    def test_classify_draft_playlist_log_is_stranded_even_with_matching_state(self):
        log = self.make_log(date(2027, 4, 2), 5, status="draft")
        item = LogItem.objects.select_related("playlist_log").get(pk=self.make_item(log, 0).pk)
        state = {"date": "2027-04-02", "hour": 5, "decks": {}, "queue": []}
        self.assertEqual(classify_log_item(item, state), "STRANDED")

    def test_classify_airing(self):
        log = self.make_log(date(2027, 4, 2), 5)
        item = LogItem.objects.select_related("playlist_log").get(pk=self.make_item(log, 0).pk)
        state = {"date": "2027-04-02", "hour": 5, "decks": {"A": {"log_item_id": item.id}}, "queue": []}
        self.assertEqual(classify_log_item(item, state), "AIRING")

    def test_classify_queued(self):
        log = self.make_log(date(2027, 4, 2), 5)
        item = LogItem.objects.select_related("playlist_log").get(pk=self.make_item(log, 0).pk)
        state = {"date": "2027-04-02", "hour": 5, "decks": {}, "queue": [{"item_id": item.id}]}
        self.assertEqual(classify_log_item(item, state), "QUEUED")

    def test_classify_future_hour_not_yet_loaded_by_engine(self):
        log = self.make_log(date(2027, 4, 2), 9)
        item = LogItem.objects.select_related("playlist_log").get(pk=self.make_item(log, 0).pk)
        state = {"date": "2027-04-02", "hour": 5, "decks": {}, "queue": []}
        self.assertEqual(classify_log_item(item, state), "FUTURE")

    def test_classify_stranded_when_active_hour_and_absent(self):
        log = self.make_log(date(2027, 4, 2), 5)
        item = LogItem.objects.select_related("playlist_log").get(pk=self.make_item(log, 0).pk)
        state = {"date": "2027-04-02", "hour": 5, "decks": {}, "queue": []}
        self.assertEqual(classify_log_item(item, state), "STRANDED")

    def test_classify_unknown_when_state_missing(self):
        log = self.make_log(date(2027, 4, 2), 5)
        item = LogItem.objects.select_related("playlist_log").get(pk=self.make_item(log, 0).pk)
        self.assertEqual(classify_log_item(item, None), "UNKNOWN")


class SchedulerBugRegressionTests(WebRequestFixtureMixin, TransactionTestCase):
    """One test per confirmed bug from the multi-round review -- see
    the plan file's Design section 2 for the full narrative of each."""

    def test_bug_a_returns_a_different_object_caller_object_untouched(self):
        log = self.make_log(date(2027, 4, 3), 5)
        original_track = self.make_track(title="Rotation Pick")
        item = self.make_item(log, 0, scheduled_time=timezone.now(), track=original_track)
        requested_track = self.make_track(title="Requested Song")
        self.make_request(requested_track, status="pending", submitted_at=timezone.now() - timedelta(hours=1))

        result = maybe_schedule_song_request(item)

        self.assertIsNot(result, item)
        self.assertEqual(result.track_id, requested_track.id)
        self.assertEqual(item.track_id, original_track.id, "caller's original object must be left untouched")

    def test_bug_a2_lock_contention_returns_sentinel_not_stale_data(self):
        log = self.make_log(date(2027, 4, 3), 10)
        rotation_track = self.make_track(title="Rotation Pick")
        requested_track = self.make_track(title="Requested")
        item = self.make_item(log, 0, scheduled_time=timezone.now(), track=rotation_track)
        self.make_request(requested_track, status="pending", submitted_at=timezone.now() - timedelta(hours=1))

        holder_ready = threading.Event()
        release_event = threading.Event()

        def hold_lock():
            close_old_connections()
            with transaction.atomic():
                LogItem.objects.select_for_update().get(pk=item.pk)
                holder_ready.set()
                release_event.wait(timeout=5)
            close_old_connections()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        holder_ready.wait(timeout=5)
        try:
            result = maybe_schedule_song_request(item)
        finally:
            release_event.set()
            holder.join(timeout=10)

        self.assertIs(result, SCHEDULING_CONTENDED)
        self.assertEqual(SongRequest.objects.filter(status="scheduled").count(), 0)

    def test_bug_b_second_call_does_not_overwrite_an_already_reserved_slot(self):
        log = self.make_log(date(2027, 4, 3), 6)
        track_a = self.make_track(title="Song A")
        track_b = self.make_track(title="Song B")
        item = self.make_item(log, 0, scheduled_time=timezone.now(), track=self.make_track(title="Rotation Pick"))
        req_a = self.make_request(track_a, status="pending", submitted_at=timezone.now() - timedelta(hours=2))
        req_b = self.make_request(track_b, status="pending", submitted_at=timezone.now() - timedelta(hours=1))

        result1 = maybe_schedule_song_request(item)
        result2 = maybe_schedule_song_request(result1)

        req_a.refresh_from_db()
        req_b.refresh_from_db()
        self.assertEqual(result2.track_id, track_a.id)
        self.assertEqual(req_a.status, "scheduled")
        self.assertEqual(req_b.status, "pending")

    def test_bug_c_capacity_serialized_across_different_logitems_same_hour(self):
        self.cfg.max_fulfilled_per_hour = 1
        self.cfg.save()
        log = self.make_log(date(2027, 4, 3), 7)
        item1 = self.make_item(log, 0, scheduled_time=timezone.now(), track=self.make_track(title="Rotation 1"))
        item2 = self.make_item(log, 1, scheduled_time=timezone.now(), track=self.make_track(title="Rotation 2"))
        track_a = self.make_track(title="A")
        track_b = self.make_track(title="B")
        self.make_request(track_a, status="pending", submitted_at=timezone.now() - timedelta(hours=2))
        self.make_request(track_b, status="pending", submitted_at=timezone.now() - timedelta(hours=1))

        barrier = threading.Barrier(2, timeout=5)

        def run(item):
            close_old_connections()
            barrier.wait()
            maybe_schedule_song_request(item)
            close_old_connections()

        t1 = threading.Thread(target=run, args=(item1,))
        t2 = threading.Thread(target=run, args=(item2,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(
            SongRequest.objects.filter(status="scheduled").count(), 1,
            "cap=1 must allow exactly one scheduling across two LogItems in the same hour",
        )

    def test_bug_d_organic_match_blocked_at_full_capacity(self):
        self.cfg.max_fulfilled_per_hour = 1
        self.cfg.save()
        log = self.make_log(date(2027, 4, 3), 8)
        track_x = self.make_track(title="X")
        track_y = self.make_track(title="Y")

        item_used = self.make_item(log, 0, scheduled_time=timezone.now(), track=self.make_track(title="Rotation Pick"))
        req_x = self.make_request(track_x, status="pending", submitted_at=timezone.now() - timedelta(hours=2))
        maybe_schedule_song_request(item_used)
        req_x.refresh_from_db()
        self.assertEqual(req_x.status, "scheduled")

        # A second LogItem already organically holds track_y (rotation
        # picked it) while a separate request for that exact song waits.
        item_organic = self.make_item(log, 1, scheduled_time=timezone.now(), track=track_y)
        req_y = self.make_request(track_y, status="pending", submitted_at=timezone.now() - timedelta(hours=1))

        maybe_schedule_song_request(item_organic)

        req_y.refresh_from_db()
        self.assertEqual(req_y.status, "pending", "organic match must not be scheduled once capacity is full")

    def test_bug_d_organic_match_scheduled_when_capacity_available(self):
        log = self.make_log(date(2027, 4, 3), 9)
        track = self.make_track(title="Y2")
        item_organic = self.make_item(log, 0, scheduled_time=timezone.now(), track=track)
        req = self.make_request(track, status="pending", submitted_at=timezone.now() - timedelta(hours=1))

        maybe_schedule_song_request(item_organic)

        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")
        self.assertEqual(req.log_item_id, item_organic.id)

    def test_collapse_onto_already_reserved_slot_is_free_even_at_capacity(self):
        self.cfg.max_fulfilled_per_hour = 1
        self.cfg.save()
        log = self.make_log(date(2027, 4, 3), 13)
        track = self.make_track(title="Shared")
        item = self.make_item(log, 0, scheduled_time=timezone.now(), track=self.make_track(title="Rotation Pick"))
        req1 = self.make_request(track, status="pending", submitted_at=timezone.now() - timedelta(hours=2))
        maybe_schedule_song_request(item)
        req1.refresh_from_db()
        self.assertEqual(req1.status, "scheduled")

        req2 = self.make_request(track, status="pending", submitted_at=timezone.now() - timedelta(hours=1))
        maybe_schedule_song_request(LogItem.objects.get(pk=item.pk))

        req2.refresh_from_db()
        self.assertEqual(req2.status, "scheduled")
        self.assertEqual(req2.log_item_id, item.id)

    def test_bug_e_advisory_estimate_excludes_already_reserved_logitem(self):
        log = self.make_log(date(2027, 4, 3), 11)
        item = self.make_item(log, 0, scheduled_time=timezone.now() + timedelta(minutes=5))
        track_a = self.make_track(title="ReservedFor")
        track_b = self.make_track(title="StillWaiting")
        self.make_request(
            track_a, status="scheduled", log_item=item, scheduled_at=timezone.now(),
            submitted_at=timezone.now() - timedelta(hours=1),
        )
        item.track = track_a
        item.save(update_fields=["track"])
        req_b = self.make_request(track_b, status="pending", submitted_at=timezone.now() - timedelta(minutes=30))

        RefreshCommand().handle()

        req_b.refresh_from_db()
        self.assertEqual(req_b.status, "no_slot_soon")
        self.assertIsNone(req_b.estimated_play_time)

    def test_in_transaction_revalidation_rejects_stale_caller_snapshot(self):
        log = self.make_log(date(2027, 4, 3), 12)
        track = self.make_track()
        item = self.make_item(log, 0, scheduled_time=timezone.now(), track=track)
        stale_caller_copy = LogItem.objects.get(pk=item.pk)
        item.played_at = timezone.now()
        item.save(update_fields=["played_at"])
        self.make_request(self.make_track(title="Late Request"), status="pending",
                           submitted_at=timezone.now() - timedelta(hours=1))

        result = maybe_schedule_song_request(stale_caller_copy)

        self.assertIsNotNone(result.played_at)
        self.assertEqual(SongRequest.objects.filter(status="scheduled").count(), 0)


class DriftedScheduledTimeCandidateTests(WebRequestFixtureMixin, TransactionTestCase):
    """2026-08-20 fix: refresh_song_request_statuses' candidate queries
    used to bound scheduled_time by `>= now`. scheduled_time is a
    static build-time estimate that routinely drifts behind real
    playback over the course of an hour (ordinary variance, not a
    fault condition) -- once a not-yet-played item's own scheduled_time
    slipped into the past relative to `now`, it silently dropped out
    of every candidate query, even though it was often the very next
    thing about to air. Confirmed live: a request submitted ~2 minutes
    before its only eligible slot showed "no slot soon" the entire
    time, because that slot's scheduled_time had already drifted ~4
    minutes behind schedule -- only the engine's own last-second
    maybe_schedule_song_request safety net (zero lead time) ultimately
    fulfilled it.

    Fix: bound by current_hour_start (the floor of the current
    station-local hour) instead of `now`, keep played_at__isnull=True
    and _is_dead_slot as the mechanisms that actually reject abandoned
    rows (a bare `now`-less time bound alone would wrongly let
    long-abandoned historical LogItems back in as candidates)."""

    def test_drifted_current_hour_item_still_proactively_scheduled(self):
        """Requirement 1: real scheduling pass (fulfillment_candidates).

        Time is frozen at 5 minutes past the top of the hour -- an
        offset relative to real timezone.now() would be flaky
        depending on exactly when the test happens to run (a "5
        minutes ago" item lands in the PREVIOUS hour, before
        current_hour_start, whenever the real clock is within the
        first 5 minutes of an hour)."""
        fake_now = timezone.make_aware(datetime(2027, 6, 20, 9, 5, 0))
        current_log = self.make_log(date(2027, 6, 20), 9)
        track = self.make_track(title="Drifted Real")
        item = self.make_item(
            current_log, 0, scheduled_time=timezone.make_aware(datetime(2027, 6, 20, 9, 0, 0)),
            track=self.make_track(title="Rotation Pick"),
        )
        self.write_state(queue=[{"item_id": item.id}], date_str="2027-06-20", hour=9)
        req = self.make_request(
            track, status="pending", submitted_at=timezone.make_aware(datetime(2027, 6, 20, 9, 3, 0)),
        )

        with patch.object(refresh_module.timezone, "now", return_value=fake_now), \
             patch.object(refresh_module.timezone, "localtime", return_value=fake_now):
            RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")
        self.assertEqual(req.log_item_id, item.id)

    def test_drifted_current_hour_item_offered_by_advisory_pass(self):
        """Requirement 2: the advisory pending/no_slot_soon estimate
        pass has its OWN, separate candidate query (`candidates`) --
        must independently see the same drifted item. Real scheduling
        (maybe_schedule_song_request) is stubbed to a no-op so this
        test observes the advisory pass's own candidate selection in
        isolation, rather than the item simply having already been
        claimed by the (also-fixed, but separately-tested) real
        scheduling pass first. Time frozen for the same reason as
        the previous test (avoid a flaky top-of-hour boundary)."""
        fake_now = timezone.make_aware(datetime(2027, 6, 20, 9, 20, 0))
        current_log = self.make_log(date(2027, 6, 20), 9)
        track = self.make_track(title="Drifted Advisory")
        item = self.make_item(
            current_log, 0, scheduled_time=timezone.make_aware(datetime(2027, 6, 20, 9, 0, 0)),
            track=self.make_track(title="Rotation Pick"),
        )
        self.write_state(queue=[{"item_id": item.id}], date_str="2027-06-20", hour=9)
        req = self.make_request(
            track, status="pending", submitted_at=timezone.make_aware(datetime(2027, 6, 20, 9, 10, 0)),
        )

        with patch.object(refresh_module.timezone, "now", return_value=fake_now), \
             patch.object(refresh_module.timezone, "localtime", return_value=fake_now), \
             patch.object(refresh_module, "maybe_schedule_song_request", lambda log_item: log_item):
            RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(req.status, "pending")
        self.assertIsNotNone(req.estimated_play_time)

    def test_past_scheduled_current_hour_item_not_queued_rejected_as_stranded(self):
        """Requirement 3: current_hour_start alone is not authoritative
        -- an item genuinely abandoned by the engine (matching
        engine_state.json's own hour, but on neither a deck nor in the
        live queue) must still be excluded via _is_dead_slot's STRANDED
        classification, not accidentally let back in. Time frozen for
        the same top-of-hour-boundary reason as the tests above."""
        fake_now = timezone.make_aware(datetime(2027, 6, 20, 9, 5, 0))
        current_log = self.make_log(date(2027, 6, 20), 9)
        track = self.make_track(title="Stranded Not Queued")
        self.make_item(
            current_log, 0, scheduled_time=timezone.make_aware(datetime(2027, 6, 20, 9, 0, 0)),
            track=self.make_track(title="Rotation Pick"),
        )
        # Engine state IS present and matches this exact hour, but the
        # item is absent from both decks and queue.
        self.write_state(queue=[], date_str="2027-06-20", hour=9)
        req = self.make_request(
            track, status="pending", submitted_at=timezone.make_aware(datetime(2027, 6, 20, 9, 3, 0)),
        )

        with patch.object(refresh_module.timezone, "now", return_value=fake_now), \
             patch.object(refresh_module.timezone, "localtime", return_value=fake_now):
            RefreshCommand().handle()

        req.refresh_from_db()
        self.assertIn(req.status, SongRequest.WAITING_STATUSES)
        self.assertIsNone(req.log_item_id)

    def test_historical_unplayed_logitem_from_earlier_hour_not_reintroduced(self):
        """Requirement 4: current_hour_start -- not a bare removal of
        the time bound -- is what keeps a long-abandoned historical
        LogItem (unplayed, but from a prior hour) out of the candidate
        query entirely, without needing engine state to reject it."""
        yesterday = timezone.localtime() - timedelta(days=1)
        old_log = self.make_log(yesterday.date(), yesterday.hour)
        track = self.make_track(title="Historical")
        self.make_item(
            old_log, 0, scheduled_time=yesterday, track=self.make_track(title="Rotation Pick"),
        )
        req = self.make_request(track, status="pending", submitted_at=timezone.now() - timedelta(minutes=2))

        RefreshCommand().handle()  # no engine_state.json -- state is None

        req.refresh_from_db()
        self.assertIn(req.status, SongRequest.WAITING_STATUSES)
        self.assertIsNone(req.log_item_id)

    def test_earlier_current_hour_reservation_still_consumes_capacity(self):
        """Requirement 5: already_used_slots_per_hour's own
        scheduled_time lower bound needed the same current_hour_start
        fix -- otherwise an earlier-in-the-hour reservation whose
        scheduled_time has since drifted into the past silently drops
        out of the capacity count as wall-clock passes it, letting the
        hour over-book past max_fulfilled_per_hour.

        Time is frozen (rather than relative to real timezone.now()):
        real wall-clock could otherwise land less than 20 minutes past
        the top of the hour, which would put earlier_item's own
        scheduled_time BEFORE current_hour_start and make this test
        flaky depending on exactly when it happens to run -- freezing
        keeps "20 minutes into a 14:00 hour" deterministic."""
        self.cfg.max_fulfilled_per_hour = 1
        self.cfg.save()
        fake_now = timezone.make_aware(datetime(2027, 6, 20, 14, 30, 0))
        current_log = self.make_log(date(2027, 6, 20), 14)

        already_track = self.make_track(title="Already Scheduled")
        earlier_item = self.make_item(
            current_log, 0, scheduled_time=timezone.make_aware(datetime(2027, 6, 20, 14, 10, 0)),
            track=already_track,
        )
        self.make_request(
            already_track, status="scheduled", log_item=earlier_item,
            scheduled_at=timezone.make_aware(datetime(2027, 6, 20, 14, 10, 0)),
            submitted_at=timezone.make_aware(datetime(2027, 6, 20, 14, 0, 0)),
        )

        later_track = self.make_track(title="Wants In Too")
        later_item = self.make_item(
            current_log, 1, scheduled_time=timezone.make_aware(datetime(2027, 6, 20, 14, 35, 0)),
            track=self.make_track(title="Rotation Pick 2"),
        )
        # BOTH items in the live queue -- earlier_item must stay
        # genuinely "scheduled" (not reverted by the reconciliation
        # pass as stranded) for this test to actually exercise capacity
        # counting rather than accidentally exercising reconciliation.
        self.write_state(
            queue=[{"item_id": earlier_item.id}, {"item_id": later_item.id}],
            date_str="2027-06-20", hour=14,
        )
        req = self.make_request(
            later_track, status="pending",
            submitted_at=timezone.make_aware(datetime(2027, 6, 20, 14, 28, 0)),
        )

        with patch.object(refresh_module.timezone, "now", return_value=fake_now), \
             patch.object(refresh_module.timezone, "localtime", return_value=fake_now):
            RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(
            req.status, "no_slot_soon",
            "capacity=1 already spent by the earlier-in-hour reservation -- must not offer a second slot this hour",
        )
        already_track_req = SongRequest.objects.get(track=already_track)
        self.assertEqual(
            already_track_req.status, "scheduled",
            "sanity check: the earlier reservation must still genuinely be 'scheduled' (not reconciled away) "
            "for this test to actually be exercising capacity counting",
        )
        self.assertIsNone(req.estimated_play_time)

    def test_advisory_recency_uses_max_now_and_scheduled_time(self):
        """Requirement 6: the advisory pass's own eligibility check must
        use max(now, item.scheduled_time), matching maybe_schedule_
        song_request's real eligibility_time exactly -- not the raw
        (possibly stale) item.scheduled_time alone. Constructed so the
        recency window's boundary falls strictly between the
        candidate's own drifted scheduled_time and the frozen "now":
        evaluating at the stale scheduled_time alone would still show
        this as recency-blocked (no_slot_soon); evaluating at
        max(now, scheduled_time) correctly shows it clear (pending).
        Real scheduling is stubbed to a no-op so this isolates the
        advisory pass's own eligibility_time computation specifically."""
        fake_now = timezone.make_aware(datetime(2027, 6, 15, 14, 30, 0))
        self.category.title_separation = 2.0
        self.category.artist_separation = 2.0
        self.category.save()

        current_log = self.make_log(date(2027, 6, 15), 14)
        conflict_log = self.make_log(date(2027, 6, 15), 12)
        track = self.make_track(title="Recency Boundary")

        # Same track played earlier: inside the 2h separation window
        # when checked against the candidate's own stale scheduled_time
        # (14:05 - 2h = 12:05, and 12:15 >= 12:05 -> blocked), but
        # outside it when checked against fake_now (14:30 - 2h = 12:30,
        # and 12:15 < 12:30 -> clear).
        self.make_item(
            conflict_log, 0, scheduled_time=timezone.make_aware(datetime(2027, 6, 15, 12, 15, 0)),
            track=track, played_at=timezone.make_aware(datetime(2027, 6, 15, 12, 15, 0)),
        )
        candidate_item = self.make_item(
            current_log, 0, scheduled_time=timezone.make_aware(datetime(2027, 6, 15, 14, 5, 0)),
            track=self.make_track(title="Rotation Pick"),
        )
        self.write_state(queue=[{"item_id": candidate_item.id}], date_str="2027-06-15", hour=14)
        req = self.make_request(
            track, status="pending",
            submitted_at=timezone.make_aware(datetime(2027, 6, 15, 14, 20, 0)),
        )

        with patch.object(refresh_module.timezone, "now", return_value=fake_now), \
             patch.object(refresh_module.timezone, "localtime", return_value=fake_now), \
             patch.object(refresh_module, "maybe_schedule_song_request", lambda log_item: log_item):
            RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(
            req.status, "pending",
            "recency at max(now, scheduled_time) should have cleared by fake_now -- evaluating at the "
            "stale scheduled_time alone would still show this as blocked (no_slot_soon)",
        )
        self.assertIsNotNone(req.estimated_play_time)

    def test_future_hour_candidate_unaffected_by_current_hour_floor(self):
        """Requirement 7: ordinary future-hour candidates -- never
        touched by the now-vs-current_hour_start distinction, since
        their scheduled_time is always ahead of both -- must keep
        working exactly as before."""
        future_dt = timezone.now() + timedelta(hours=2)
        future_local = timezone.localtime(future_dt)
        future_log = self.make_log(future_local.date(), future_local.hour)
        track = self.make_track(title="Future Unaffected")
        self.make_item(
            future_log, 0, scheduled_time=future_dt, track=self.make_track(title="Rotation Pick"),
        )
        self.cfg.lookahead_warning_minutes = 180
        self.cfg.save()
        req = self.make_request(track, status="pending", submitted_at=timezone.now() - timedelta(minutes=1))

        RefreshCommand().handle()  # no engine_state.json -- FUTURE items aren't excluded by unverifiable state alone

        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")


class MarkSongRequestsAiredTests(WebRequestFixtureMixin, TransactionTestCase):
    def test_promotes_scheduled_to_fulfilled(self):
        track = self.make_track()
        log = self.make_log(date(2027, 4, 1), 5)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item, scheduled_at=timezone.now())
        aired_at = timezone.now()

        mark_song_requests_aired(item, aired_at)

        req.refresh_from_db()
        self.assertEqual(req.status, "fulfilled")
        self.assertEqual(req.fulfilled_at, aired_at)
        self.assertEqual(req.resolved_at, aired_at)
        self.assertEqual(req.estimated_play_time, aired_at)

    def test_does_not_promote_on_track_mismatch(self):
        track_a = self.make_track(title="A")
        track_b = self.make_track(title="B")
        log = self.make_log(date(2027, 4, 1), 6)
        item = self.make_item(log, 0, track=track_b)  # slot actually holds track_b
        req = self.make_request(track_a, status="scheduled", log_item=item)

        mark_song_requests_aired(item, timezone.now())

        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")

    def test_collapses_multiple_requests_together(self):
        track = self.make_track()
        log = self.make_log(date(2027, 4, 1), 7)
        item = self.make_item(log, 0, track=track)
        req1 = self.make_request(track, status="scheduled", log_item=item)
        req2 = self.make_request(track, status="scheduled", log_item=item)

        mark_song_requests_aired(item, timezone.now())

        req1.refresh_from_db()
        req2.refresh_from_db()
        self.assertEqual(req1.status, "fulfilled")
        self.assertEqual(req2.status, "fulfilled")


class EngineCallSiteTests(TransactionTestCase):
    """Exercises _start_next_track's own logic in isolation (no real
    GStreamer) -- proves the call site correctly threads the scheduler's
    return value through to _create_deck, and correctly skips deck
    creation entirely on SCHEDULING_CONTENDED."""

    def test_uses_scheduler_return_value_not_the_stale_original(self):
        stand_in = make_engine_stand_in()
        original_item = MagicMock(id=1)
        returned_item = MagicMock(id=1)
        stand_in._next_queue_item = MagicMock(return_value=(original_item, False))
        stand_in._create_deck = MagicMock(return_value=MagicMock())

        with patch.object(eng_module, "maybe_schedule_song_request", return_value=returned_item):
            eng_module.PlaybackEngine._start_next_track(stand_in, slot="A")

        stand_in._create_deck.assert_called_once_with("A", returned_item)

    def test_scheduling_contended_skips_create_deck_and_retries_next_item(self):
        stand_in = make_engine_stand_in()
        contended_item = MagicMock(id=1)
        stand_in._next_queue_item = MagicMock(side_effect=[(contended_item, False), (None, False)])
        stand_in._create_deck = MagicMock()
        stand_in._on_log_exhausted = MagicMock()

        with patch.object(eng_module, "maybe_schedule_song_request", return_value=SCHEDULING_CONTENDED):
            eng_module.PlaybackEngine._start_next_track(stand_in, slot="A")

        stand_in._create_deck.assert_not_called()
        stand_in._on_log_exhausted.assert_called_once_with("A")
        self.assertEqual(stand_in._next_queue_item.call_count, 2)

    def test_mark_song_requests_aired_gated_on_played_at_write_succeeding(self):
        """Static confirmation that _create_deck only calls
        mark_song_requests_aired after played_at itself was
        successfully written -- not merely attempted, and not blocked
        by an unrelated Track counter-update failure. Full GStreamer
        simulation isn't practical here; this mirrors the established
        static-source-check pattern used elsewhere in this project's
        engine tests for the same kind of guard-structure proof."""
        src = inspect.getsource(eng_module.PlaybackEngine._create_deck)
        self.assertIn("played_at_written = False", src)
        self.assertIn("played_at_written = True", src)
        self.assertIn("if played_at_written:", src)
        self.assertIn("mark_song_requests_aired(", src)


class ReconciliationPassTests(WebRequestFixtureMixin, TransactionTestCase):
    def test_self_heal_promotes_aired_scheduled_request(self):
        log = self.make_log(date(2027, 4, 4), 5)
        track = self.make_track()
        item = self.make_item(log, 0, track=track, played_at=timezone.now())
        req = self.make_request(track, status="scheduled", log_item=item, scheduled_at=timezone.now())

        RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(req.status, "fulfilled")
        self.assertIsNotNone(req.fulfilled_at)

    def test_self_heal_reverts_on_track_mismatch_even_if_something_aired(self):
        log = self.make_log(date(2027, 4, 4), 6)
        track_a = self.make_track(title="A")
        track_b = self.make_track(title="B")
        item = self.make_item(log, 0, track=track_b, played_at=timezone.now())
        req = self.make_request(track_a, status="scheduled", log_item=item, scheduled_at=timezone.now())

        RefreshCommand().handle()

        # Reverted to waiting by the self-heal pass; whether it's still
        # "pending" or already demoted to "no_slot_soon" depends on
        # whether the SAME run's later no-open-candidate re-evaluation
        # also ran over it -- both are correct "back to waiting" outcomes.
        req.refresh_from_db()
        self.assertIn(req.status, SongRequest.WAITING_STATUSES)
        self.assertIsNone(req.log_item_id)

    def test_unavailable_track_marks_scheduled_request_unavailable(self):
        log = self.make_log(date(2027, 4, 4), 7)
        track = self.make_track(ready2air=False)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item, scheduled_at=timezone.now())

        RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(req.status, "unavailable")
        self.assertIsNone(req.log_item_id)

    def test_stranded_scheduled_request_requeued_not_reclaimed_same_run(self):
        old_log = self.make_log(date(2027, 4, 4), 5)
        track = self.make_track()
        item = self.make_item(old_log, 0, scheduled_time=timezone.now() + timedelta(seconds=5), track=track)
        req = self.make_request(track, status="scheduled", log_item=item, scheduled_at=timezone.now())
        self.write_state(date_str="2027-04-04", hour=6)  # active hour has already moved past hour 5

        RefreshCommand().handle()

        # Same reasoning as the track-mismatch test above -- "pending"
        # or "no_slot_soon" are both correct "back to waiting" outcomes.
        req.refresh_from_db()
        self.assertIn(req.status, SongRequest.WAITING_STATUSES)
        self.assertIsNone(req.log_item_id)

    def test_future_hour_candidate_still_schedulable_when_engine_state_missing(self):
        local_now = timezone.localtime()
        future_dt = local_now + timedelta(hours=2)
        future_log = self.make_log(future_dt.date(), future_dt.hour)
        track = self.make_track(title="Future")
        self.make_item(future_log, 0, scheduled_time=timezone.now() + timedelta(hours=2),
                        track=self.make_track(title="Rotation Pick"))
        req = self.make_request(track, status="pending", submitted_at=timezone.now() - timedelta(minutes=10))
        self.cfg.lookahead_warning_minutes = 180
        self.cfg.save()

        RefreshCommand().handle()  # no engine_state.json written -- state is None

        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")

    def test_current_hour_candidate_not_scheduled_when_engine_state_missing(self):
        local_now = timezone.localtime()
        current_log = self.make_log(local_now.date(), local_now.hour)
        track = self.make_track(title="Current")
        self.make_item(current_log, 0, scheduled_time=timezone.now() + timedelta(seconds=5),
                        track=self.make_track(title="Rotation Pick"))
        req = self.make_request(track, status="pending", submitted_at=timezone.now() - timedelta(minutes=10))

        RefreshCommand().handle()  # no engine_state.json written -- state is None

        # Real scheduling and the advisory estimate both correctly
        # treat this unverifiable current-hour candidate as excluded --
        # since it's the fixture's only candidate, the request lands on
        # "no_slot_soon" (no eligible slot found) rather than staying
        # "pending" with an estimate. Either WAITING_STATUSES value is
        # a correct "not scheduled" outcome; what matters is it's NOT
        # "scheduled".
        req.refresh_from_db()
        self.assertIn(req.status, SongRequest.WAITING_STATUSES)

    def test_expiry_uses_original_submitted_at_after_repeated_requeue(self):
        self.cfg.expire_after_hours = 1
        self.cfg.save()
        track = self.make_track()
        old_submitted_at = timezone.now() - timedelta(hours=2)
        req = self.make_request(track, status="pending", submitted_at=old_submitted_at)

        RefreshCommand().handle()

        req.refresh_from_db()
        self.assertEqual(req.status, "expired")


class DumpPayloadTests(WebRequestFixtureMixin, TransactionTestCase):
    def test_payload_includes_complete_field_set_with_explicit_nulls(self):
        req = self.make_request(self.make_track(), status="pending")

        out = StringIO()
        call_command("dump_song_request_statuses", stdout=out)
        payload = json.loads(out.getvalue())

        matching = [u for u in payload["updates"] if u["external_request_id"] == req.external_request_id]
        self.assertEqual(len(matching), 1)
        update = matching[0]
        for key in ("status", "estimated_play_time", "scheduled_at", "fulfilled_at", "status_updated_at"):
            self.assertIn(key, update)
        self.assertIsNone(update["estimated_play_time"])
        self.assertIsNone(update["scheduled_at"])
        self.assertIsNone(update["fulfilled_at"])
        self.assertIsNotNone(update["status_updated_at"])

    def test_scheduled_status_reported_like_pending(self):
        track = self.make_track()
        log = self.make_log(date(2027, 4, 5), 5)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(track, status="scheduled", log_item=item, scheduled_at=timezone.now())

        out = StringIO()
        call_command("dump_song_request_statuses", stdout=out)
        payload = json.loads(out.getvalue())

        ids = [u["external_request_id"] for u in payload["updates"]]
        self.assertIn(req.external_request_id, ids)


class MigrationBackfillTests(WebRequestFixtureMixin, TransactionTestCase):
    def test_aired_row_stays_fulfilled_with_corrected_timestamps(self):
        track = self.make_track()
        log = self.make_log(date(2027, 4, 6), 5)
        played_at = timezone.now() - timedelta(minutes=10)
        item = self.make_item(log, 0, track=track, played_at=played_at)
        old_assigned_at = timezone.now() - timedelta(minutes=20)
        req = SongRequest.objects.create(
            external_request_id="mig-1", track=track, status="fulfilled",
            submitted_at=old_assigned_at, log_item=item,
            fulfilled_at=old_assigned_at, resolved_at=old_assigned_at,
            estimated_play_time=old_assigned_at,
        )

        _migration_0006.backfill_scheduled_status(_FakeApps(), None)

        req.refresh_from_db()
        self.assertEqual(req.status, "fulfilled")
        self.assertEqual(req.scheduled_at, old_assigned_at)
        self.assertEqual(req.fulfilled_at, played_at)
        self.assertEqual(req.resolved_at, played_at)

    def test_unaired_row_becomes_scheduled(self):
        track = self.make_track()
        log = self.make_log(date(2027, 4, 6), 6)
        item = self.make_item(log, 0, track=track)
        old_assigned_at = timezone.now() - timedelta(minutes=5)
        req = SongRequest.objects.create(
            external_request_id="mig-2", track=track, status="fulfilled",
            submitted_at=old_assigned_at, log_item=item,
            fulfilled_at=old_assigned_at, resolved_at=old_assigned_at,
        )

        _migration_0006.backfill_scheduled_status(_FakeApps(), None)

        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")
        self.assertEqual(req.scheduled_at, old_assigned_at)
        self.assertIsNone(req.fulfilled_at)
        self.assertIsNone(req.resolved_at)

    def test_null_log_item_row_untouched(self):
        old_assigned_at = timezone.now() - timedelta(hours=1)
        req = SongRequest.objects.create(
            external_request_id="mig-3", track=self.make_track(), status="fulfilled",
            submitted_at=old_assigned_at, log_item=None,
            fulfilled_at=old_assigned_at, resolved_at=old_assigned_at,
        )

        _migration_0006.backfill_scheduled_status(_FakeApps(), None)

        req.refresh_from_db()
        self.assertEqual(req.status, "fulfilled")
        self.assertEqual(req.fulfilled_at, old_assigned_at)


class OrphanCommandTests(WebRequestFixtureMixin, TransactionTestCase):
    def _make_orphan(self, age_hours, eta_offset_minutes, ext_id):
        track = self.make_track()
        submitted_at = timezone.now() - timedelta(hours=age_hours)
        eta = timezone.now() + timedelta(minutes=eta_offset_minutes) if eta_offset_minutes is not None else None
        return SongRequest.objects.create(
            external_request_id=ext_id, track=track, status="fulfilled",
            submitted_at=submitted_at, log_item=None,
            fulfilled_at=submitted_at, resolved_at=submitted_at,
            estimated_play_time=eta,
        )

    def test_strong_evidence_requeued_without_flag(self):
        req = self._make_orphan(age_hours=1, eta_offset_minutes=10, ext_id="orphan-strong")
        call_command("reconcile_orphaned_fulfilled_requests", stdout=StringIO())
        req.refresh_from_db()
        self.assertEqual(req.status, "pending")

    def test_ambiguous_recent_not_touched_without_flag(self):
        req = self._make_orphan(age_hours=1, eta_offset_minutes=-10, ext_id="orphan-ambig")
        call_command("reconcile_orphaned_fulfilled_requests", stdout=StringIO())
        req.refresh_from_db()
        self.assertEqual(req.status, "fulfilled")

    def test_ambiguous_recent_requeued_with_flag(self):
        req = self._make_orphan(age_hours=1, eta_offset_minutes=-10, ext_id="orphan-ambig2")
        call_command("reconcile_orphaned_fulfilled_requests", "--requeue-all-recent", stdout=StringIO())
        req.refresh_from_db()
        self.assertEqual(req.status, "pending")

    def test_old_row_never_touched_even_with_flag(self):
        req = self._make_orphan(age_hours=100, eta_offset_minutes=10, ext_id="orphan-old")
        call_command("reconcile_orphaned_fulfilled_requests", "--requeue-all-recent", stdout=StringIO())
        req.refresh_from_db()
        self.assertEqual(req.status, "fulfilled")


class AdminSaveModelTests(WebRequestFixtureMixin, TransactionTestCase):
    def test_save_model_clears_fields_and_bumps_status_updated_at_on_regression(self):
        track = self.make_track()
        log = self.make_log(date(2027, 4, 7), 5)
        item = self.make_item(log, 0, track=track)
        req = self.make_request(
            track, status="scheduled", log_item=item, scheduled_at=timezone.now(),
            estimated_play_time=timezone.now(),
        )
        old_status_updated_at = req.status_updated_at

        admin_instance = SongRequestAdmin(SongRequest, admin.site)
        req.status = "pending"
        admin_instance.save_model(request=None, obj=req, form=None, change=True)

        req.refresh_from_db()
        self.assertEqual(req.status, "pending")
        self.assertIsNone(req.log_item_id)
        self.assertIsNone(req.scheduled_at)
        self.assertIsNone(req.estimated_play_time)
        self.assertGreater(req.status_updated_at, old_status_updated_at)
