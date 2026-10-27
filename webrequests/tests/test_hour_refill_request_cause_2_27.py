"""Roadmap 2.27 Phase A: hour-refill cause capture at the web-request swap.

Drives the real maybe_schedule_song_request() locked transaction and
checks that a cause is recorded exactly when a committed swap replaces a
scheduled music Track with a different, strictly shorter one (and capture
is deliberately enabled), and that capture -- including injected failures
-- never changes what r0119 scheduling does.

TransactionTestCase, like test_request_scheduling_lifecycle.py: several
tests hold row locks from a second connection."""
import threading
from datetime import date, timedelta
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from library.models import (
    Category, CategoryKind, HourRefillCause, HourRefillPolicy, LogFillConfig, PlayEvent, Track,
)
from library.services.hour_refill_causality import (
    build_causality_trace,
    load_cause_records,
    request_substitution_source_key,
)
from monitoring.models import SystemEvent
from webrequests.models import SongRequest
from webrequests.services import SCHEDULING_CONTENDED, maybe_schedule_song_request
import webrequests.services as services_module

from webrequests.tests.test_request_scheduling_lifecycle import WebRequestFixtureMixin

PRIVATE_NAME = "Zelda Privateperson"
PRIVATE_MESSAGE = "secret dedication for my sister"


class HourRefillRequestCauseTests(WebRequestFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        kind = CategoryKind.objects.get(code="music")
        # The requested Track's own primary category -- deliberately NOT the slot's.
        self.request_category = Category.objects.create(code="TESTREQ", name="Requested", kind=kind)
        self.log = self.make_log(date(2027, 5, 4), 9)

    # -- fixtures -----------------------------------------------------------

    def enable_capture(self, **flags):
        values = dict(enabled=True, capture_request_substitution=True)
        values.update(flags)
        HourRefillPolicy(**values).save()

    def track(self, title, *, duration=300.0, cue_in=0.0, next_start=None, category=None):
        track = self.make_track(title=title)
        Track.objects.filter(pk=track.pk).update(
            duration_seconds=duration, cue_in_seconds=cue_in, next_start_seconds=next_start,
            category=category or self.category,
        )
        track.refresh_from_db()
        return track

    def slot(self, track, position=0, log=None, **extra):
        item = self.make_item(log or self.log, position, scheduled_time=timezone.now(), track=track, **extra)
        # The log builder always fills the identity snapshot; mirror it.
        type(item).objects.filter(pk=item.pk).update(track_title=track.title, track_artist=track.artist.name)
        item.refresh_from_db()
        return item

    def request(self, track, minutes_ago=60, **extra):
        return self.make_request(
            track, status="pending", submitted_at=timezone.now() - timedelta(minutes=minutes_ago), **extra,
        )

    def shorter_swap_fixture(self):
        # Effective airtimes: 300 - 2 = 298 (next_start 300) vs 215 - 5 = 210.
        scheduled = self.track("Scheduled Long", duration=310.0, cue_in=2.0, next_start=300.0)
        requested = self.track("Requested Short", duration=220.0, cue_in=5.0, next_start=215.0,
                               category=self.request_category)
        item = self.slot(scheduled)
        req = self.request(requested, requester_name=PRIVATE_NAME, dedication_message=PRIVATE_MESSAGE)
        return scheduled, requested, item, req

    # -- the eligible cause ---------------------------------------------------

    def test_shorter_substitution_records_one_atomic_cause(self):
        self.enable_capture()
        scheduled, requested, item, req = self.shorter_swap_fixture()

        result = maybe_schedule_song_request(item)

        self.assertEqual(result.track_id, requested.id)
        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")
        self.assertEqual(req.log_item_id, item.id)
        cause = HourRefillCause.objects.get()
        self.assertEqual(cause.cause_type, "request_substitution")
        self.assertEqual(cause.state, "recorded")
        self.assertEqual(cause.source_key, request_substitution_source_key(item.id, req.id, scheduled.id, requested.id))
        self.assertEqual(
            (cause.playlist_log_id_snapshot, cause.log_date, cause.log_hour, cause.log_item_id_snapshot,
             cause.log_item_position),
            (self.log.id, self.log.date, 9, item.id, 0),
        )
        # Scheduled slot category, not the requested Track's own category.
        self.assertEqual(
            (cause.scheduled_category_id_snapshot, cause.scheduled_category_code, cause.scheduled_category_kind_code),
            (self.category.id, "TESTMUS", "music"),
        )
        self.assertEqual(
            (cause.original_track_id_snapshot, cause.original_track_title, cause.original_track_artist),
            (scheduled.id, "Scheduled Long", "Test Artist"),
        )
        self.assertEqual(cause.original_effective_airtime_seconds, 298.0)
        self.assertEqual(cause.replacement_track_id_snapshot, requested.id)
        self.assertEqual(cause.replacement_effective_airtime_seconds, 210.0)
        self.assertEqual(cause.observed_shortening_seconds, 88.0)
        self.assertEqual(cause.song_request_id_snapshot, req.id)
        self.assertEqual(cause.occurred_at, req.scheduled_at)

    def test_capture_creates_no_play_event_and_no_false_fulfillment(self):
        self.enable_capture()
        _scheduled, _requested, item, req = self.shorter_swap_fixture()
        maybe_schedule_song_request(item)
        req.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(req.status, "scheduled")
        self.assertIsNone(req.fulfilled_at)
        self.assertIsNone(item.played_at)
        self.assertIsNone(item.playback_claimed_at)
        self.assertEqual(PlayEvent.objects.count(), 0)

    def test_blank_slot_snapshot_falls_back_to_the_displaced_track_title(self):
        self.enable_capture()
        _scheduled, _requested, item, _req = self.shorter_swap_fixture()
        type(item).objects.filter(pk=item.pk).update(track_title="", track_artist="")
        item.refresh_from_db()
        maybe_schedule_song_request(item)
        cause = HourRefillCause.objects.get()
        self.assertEqual((cause.original_track_title, cause.original_track_artist), ("Scheduled Long", ""))

    def test_no_private_request_text_in_evidence(self):
        self.enable_capture()
        *_rest, item, _req = self.shorter_swap_fixture()
        maybe_schedule_song_request(item)
        row = HourRefillCause.objects.values().get()
        blob = repr(row)
        self.assertNotIn(PRIVATE_NAME, blob)
        self.assertNotIn(PRIVATE_MESSAGE, blob)
        self.assertNotIn("ext-", blob)  # not even the public site's request id

    # -- non-causes -----------------------------------------------------------

    def assert_swap_without_cause(self, item, expected_track):
        result = maybe_schedule_song_request(item)
        self.assertEqual(result.track_id, expected_track.id)
        self.assertEqual(HourRefillCause.objects.count(), 0)
        # Not attempted at all -- not merely rejected by a DB constraint.
        self.assertFalse(SystemEvent.objects.filter(title__startswith="Hour-refill cause").exists())
        return result

    def test_equal_and_longer_substitutions_are_not_causes(self):
        self.enable_capture()
        for label, (dur, nxt) in {"equal": (310.0, 300.0), "longer": (400.0, 390.0)}.items():
            with self.subTest(label):
                SongRequest.objects.all().delete()
                scheduled = self.track(f"Sched {label}", duration=310.0, cue_in=0.0, next_start=300.0)
                requested = self.track(f"Req {label}", duration=dur, cue_in=0.0, next_start=nxt)
                item = self.slot(scheduled, position=10 + len(label))
                self.request(requested)
                self.assert_swap_without_cause(item, requested)

    def test_same_track_request_is_not_a_cause(self):
        self.enable_capture()
        scheduled = self.track("Already There", duration=300.0)
        item = self.slot(scheduled)
        req = self.request(scheduled)
        self.assert_swap_without_cause(item, scheduled)
        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")

    def test_collapsed_request_adds_no_second_cause(self):
        self.enable_capture()
        _scheduled, requested, item, first = self.shorter_swap_fixture()
        maybe_schedule_song_request(item)
        second = self.request(requested, minutes_ago=1)
        maybe_schedule_song_request(item)
        second.refresh_from_db()
        self.assertEqual(second.status, "scheduled")
        self.assertEqual(second.log_item_id, item.id)
        self.assertEqual(HourRefillCause.objects.count(), 1)
        self.assertEqual(HourRefillCause.objects.get().song_request_id_snapshot, first.id)

    def test_claimed_and_played_slots_are_not_causes(self):
        self.enable_capture()
        for label, extra in {"claimed": {"playback_claimed_at": timezone.now()},
                             "played": {"played_at": timezone.now()}}.items():
            with self.subTest(label):
                scheduled = self.track(f"Sched {label}", duration=300.0)
                requested = self.track(f"Req {label}", duration=120.0)
                item = self.slot(scheduled, position=20 + len(label))
                type(item).objects.filter(pk=item.pk).update(**extra)
                self.request(requested)
                result = maybe_schedule_song_request(item)
                self.assertEqual(result.track_id, scheduled.id)
        self.assertEqual(HourRefillCause.objects.count(), 0)

    def test_non_music_slot_is_not_a_cause(self):
        self.enable_capture()
        psa_kind, _ = CategoryKind.objects.get_or_create(code="psa", defaults={"name": "PSA"})
        psa = Category.objects.create(code="TESTPSA", name="PSA", kind=psa_kind)
        scheduled = self.track("PSA Spot", duration=60.0)
        item = self.slot(scheduled)
        type(item).objects.filter(pk=item.pk).update(category=psa)
        item.refresh_from_db()
        self.request(self.track("Short", duration=30.0))
        result = maybe_schedule_song_request(item)
        self.assertEqual(result.track_id, scheduled.id)
        self.assertEqual(HourRefillCause.objects.count(), 0)

    def test_future_hour_swap_is_recorded_as_a_future_cause_only(self):
        self.enable_capture()
        future_log = self.make_log(date(2027, 5, 4), 11)
        scheduled = self.track("Future Long", duration=300.0)
        requested = self.track("Future Short", duration=200.0)
        item = self.slot(scheduled, log=future_log)
        self.request(requested)
        maybe_schedule_song_request(item)
        cause = HourRefillCause.objects.get()
        self.assertEqual((cause.log_date, cause.log_hour), (date(2027, 5, 4), 11))
        trace = build_causality_trace(load_cause_records(), active_log_date=date(2027, 5, 4), active_log_hour=9)
        self.assertEqual(trace.causes, ())
        self.assertEqual(len(trace.other_hour_causes), 1)
        self.assertEqual(trace.observed_shortening_seconds, 0.0)

    def test_lock_contention_records_nothing(self):
        self.enable_capture()
        _scheduled, _requested, item, _req = self.shorter_swap_fixture()
        ready, release = threading.Event(), threading.Event()

        def hold_lock():
            close_old_connections()
            with transaction.atomic():
                type(item).objects.select_for_update().get(pk=item.pk)
                ready.set()
                release.wait(timeout=5)
            close_old_connections()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        ready.wait(timeout=5)
        try:
            result = maybe_schedule_song_request(item)
        finally:
            release.set()
            holder.join(timeout=10)
        self.assertIs(result, SCHEDULING_CONTENDED)
        self.assertEqual(HourRefillCause.objects.count(), 0)

    def test_rolled_back_swap_leaves_no_cause(self):
        """A failure AFTER the cause's savepoint (here in the collapse step)
        rolls back the whole transaction, cause included -- exactly as the
        swap itself is rolled back."""
        self.enable_capture()
        scheduled, requested, item, _req = self.shorter_swap_fixture()
        self.request(requested, minutes_ago=1)  # gives the collapse step work
        real = services_module.estimate_air_time
        calls = []

        def fail_second(log_item):
            calls.append(log_item)
            if len(calls) > 1:
                raise RuntimeError("injected collapse failure")
            return real(log_item)

        with patch.object(services_module, "estimate_air_time", side_effect=fail_second):
            result = maybe_schedule_song_request(item)
        self.assertEqual(result.track_id, scheduled.id)  # the r0119 outer handler's original item
        item.refresh_from_db()
        self.assertEqual(item.track_id, scheduled.id)
        self.assertEqual(SongRequest.objects.filter(status="scheduled").count(), 0)
        self.assertEqual(HourRefillCause.objects.count(), 0)

    # -- evidence failures never change scheduling ---------------------------

    def test_duplicate_source_key_failure_keeps_the_swap(self):
        self.enable_capture()
        scheduled, requested, item, req = self.shorter_swap_fixture()
        key = request_substitution_source_key(item.id, req.id, scheduled.id, requested.id)
        HourRefillCause.objects.create(
            source_key=key, cause_type="request_substitution", occurred_at=timezone.now(),
            playlist_log_id_snapshot=self.log.id, log_date=self.log.date, log_hour=9,
            log_item_id_snapshot=item.id, log_item_position=0, original_effective_airtime_seconds=1.0,
            original_track_id_snapshot=1, replacement_track_id_snapshot=2,
            replacement_effective_airtime_seconds=0.5, observed_shortening_seconds=0.5,
            song_request_id_snapshot=req.id,
        )
        result = maybe_schedule_song_request(item)
        self.assertEqual(result.track_id, requested.id)
        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")
        self.assertEqual(HourRefillCause.objects.count(), 1)
        event = SystemEvent.objects.get(title="Hour-refill cause capture failed")
        self.assertEqual(event.detail["error"], "IntegrityError")
        self.assertNotIn(PRIVATE_NAME, repr(event.detail))
        self.assertNotIn(PRIVATE_MESSAGE, repr(event.detail))

    def test_preparation_db_error_keeps_the_swap(self):
        """A real aborted-statement error during preparation (as if the code
        ran before its migration) rolls back only the savepoint."""
        self.enable_capture()
        _scheduled, requested, item, req = self.shorter_swap_fixture()

        def broken_flags():
            with connection.cursor() as cur:
                cur.execute("SELECT 1 FROM library_no_such_table_2_27")
            return True, False

        with patch.object(HourRefillPolicy, "capture_flags", side_effect=broken_flags):
            result = maybe_schedule_song_request(item)
        self.assertEqual(result.track_id, requested.id)
        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")
        self.assertEqual(HourRefillCause.objects.count(), 0)
        self.assertTrue(SystemEvent.objects.filter(title="Hour-refill cause capture failed").exists())

    def test_cause_insert_lock_timeout_keeps_the_swap_and_is_not_contention(self):
        """Another uncommitted insert of the same source key makes ours wait
        on the unique index; the swap's own 250 ms lock_timeout fires inside
        the cause savepoint, which must not turn into SCHEDULING_CONTENDED."""
        self.enable_capture()
        scheduled, requested, item, req = self.shorter_swap_fixture()
        key = request_substitution_source_key(item.id, req.id, scheduled.id, requested.id)
        ready, release = threading.Event(), threading.Event()

        def hold_conflicting_insert():
            close_old_connections()
            with transaction.atomic():
                HourRefillCause.objects.create(
                    source_key=key, cause_type="request_substitution", occurred_at=timezone.now(),
                    playlist_log_id_snapshot=1, log_date=self.log.date, log_hour=9,
                    log_item_id_snapshot=item.id, log_item_position=0,
                    original_effective_airtime_seconds=1.0, original_track_id_snapshot=1,
                    replacement_track_id_snapshot=2, replacement_effective_airtime_seconds=0.5,
                    observed_shortening_seconds=0.5, song_request_id_snapshot=req.id,
                )
                ready.set()
                release.wait(timeout=5)
                transaction.set_rollback(True)
            close_old_connections()

        holder = threading.Thread(target=hold_conflicting_insert)
        holder.start()
        ready.wait(timeout=5)
        try:
            result = maybe_schedule_song_request(item)
        finally:
            release.set()
            holder.join(timeout=10)
        self.assertIsNot(result, SCHEDULING_CONTENDED)
        self.assertEqual(result.track_id, requested.id)
        req.refresh_from_db()
        self.assertEqual(req.status, "scheduled")
        self.assertEqual(HourRefillCause.objects.count(), 0)
        event = SystemEvent.objects.get(title="Hour-refill cause capture failed")
        self.assertEqual(event.detail["error"], "OperationalError")

    # -- idempotency, multiplicity, durability --------------------------------

    def test_repeated_polling_yields_one_durable_cause(self):
        self.enable_capture()
        *_rest, item, _req = self.shorter_swap_fixture()
        for _ in range(3):
            maybe_schedule_song_request(item)  # stale caller object each time, like the poller
        self.assertEqual(HourRefillCause.objects.count(), 1)

    def test_distinct_substitutions_keep_distinct_causes_in_order(self):
        self.enable_capture()
        long_a = self.track("Long A", duration=300.0)
        long_b = self.track("Long B", duration=280.0)
        short_a = self.track("Short A", duration=200.0)
        short_b = self.track("Short B", duration=250.0)
        item_a = self.slot(long_a, position=0)
        item_b = self.slot(long_b, position=1)
        self.request(short_a, minutes_ago=10)
        maybe_schedule_song_request(item_a)
        self.request(short_b, minutes_ago=5)
        maybe_schedule_song_request(item_b)
        records = load_cause_records(log_date=self.log.date, log_hour=9)
        self.assertEqual([r.log_item_id for r in records], [item_a.id, item_b.id])
        self.assertEqual([r.observed_shortening_seconds for r in records], [100.0, 30.0])
        trace = build_causality_trace(records, active_log_date=self.log.date, active_log_hour=9)
        self.assertEqual(trace.observed_shortening_seconds, 130.0)
        self.assertIsNone(trace.net_deficit_seconds)

    def test_cause_survives_reload_and_deletion_of_its_sources(self):
        self.enable_capture()
        scheduled, requested, item, req = self.shorter_swap_fixture()
        ids = (scheduled.id, requested.id, req.id, item.id)
        maybe_schedule_song_request(item)
        close_old_connections()  # a fresh connection, as after a restart
        scheduled.delete()
        req.delete()
        self.log.delete()  # cascades the LogItem
        records = load_cause_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(
            (record.original_track_id, record.replacement_track_id, record.song_request_id, record.log_item_id),
            ids,
        )
        row = HourRefillCause.objects.get()
        self.assertEqual(row.original_track_title, "Scheduled Long")
        self.assertEqual(row.log_item_id_snapshot, item.id)
        self.assertEqual(row.original_effective_airtime_seconds, 298.0)

    # -- default-Off parity with r0119 ----------------------------------------

    def test_off_parity_scheduling_identical_and_no_causes(self):
        settings = {
            "no policy row": None,
            "master off": dict(enabled=False, capture_request_substitution=True, capture_operator_eject=True),
            "request trigger off": dict(enabled=True, capture_request_substitution=False),
            "eject only": dict(enabled=True, capture_request_substitution=False, capture_operator_eject=True),
        }
        outcomes = {}
        for label, policy in settings.items():
            with self.subTest(label):
                HourRefillPolicy.objects.all().delete()
                SongRequest.objects.all().delete()
                if policy is not None:
                    HourRefillPolicy(**policy).save()
                scheduled = self.track(f"Sched {label}", duration=300.0)
                requested = self.track(f"Req {label}", duration=100.0)
                item = self.slot(scheduled, position=40 + len(outcomes))
                req = self.request(requested)
                result = maybe_schedule_song_request(item)
                req.refresh_from_db()
                outcomes[label] = (result.track_id == requested.id, req.status, req.log_item_id == item.id)
                self.assertEqual(HourRefillCause.objects.count(), 0)
        self.assertEqual(set(outcomes.values()), {(True, "scheduled", True)})
        self.assertFalse(LogFillConfig.objects.exists())

    def test_capture_does_not_create_policy_or_fill_config_rows(self):
        _scheduled, _requested, item, _req = self.shorter_swap_fixture()
        maybe_schedule_song_request(item)
        self.assertFalse(HourRefillPolicy.objects.exists())
        self.assertFalse(LogFillConfig.objects.exists())
