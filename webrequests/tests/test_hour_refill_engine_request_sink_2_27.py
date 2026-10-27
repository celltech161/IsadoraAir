"""Roadmap 2.27 Phase A: engine-path request swaps hand evidence to the
evidence worker on commit, with no added SQL on the engine thread.

The engine's last-second maybe_schedule_song_request() call keeps Codex's
guarantee (no Phase A query, savepoint or lock). With request capture On it
passes refill_evidence_sink: the swap builds evidence from objects it
already holds and registers an on-commit put_nowait. A rolled-back swap
never reaches the sink."""
import time
from datetime import date, timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from library.models import HourRefillCause, HourRefillPolicy, LogItem, Track
from library.services.hour_refill_evidence import HourRefillEvidenceRecorder
from webrequests.models import SongRequest
from webrequests.services import maybe_schedule_song_request
import webrequests.services as services_module

from webrequests.tests.test_request_scheduling_lifecycle import WebRequestFixtureMixin

QUEUE_RELATED = ("track", "track__artist", "track__category", "track__category__kind", "category", "category__kind")


class EngineRequestSinkTests(WebRequestFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.log = self.make_log(date(2027, 7, 1), 10)
        self.offers = []

    def sink(self, evidence):
        self.offers.append(evidence)
        return True

    def track(self, title, duration, next_start=None):
        track = self.make_track(title=title)
        Track.objects.filter(pk=track.pk).update(duration_seconds=duration, next_start_seconds=next_start)
        track.refresh_from_db()
        return track

    def queue_item(self, scheduled, origin=LogItem.ORIGIN_GENERATED_SCHEDULE):
        item = self.make_item(self.log, 0, scheduled_time=timezone.now(), track=scheduled)
        LogItem.objects.filter(pk=item.pk).update(
            track_title=scheduled.title, track_artist=scheduled.artist.name, origin=origin,
        )
        # Exactly how the engine's queue holds it: Track and category cached.
        return LogItem.objects.select_related(*QUEUE_RELATED).get(pk=item.pk)

    def fixture(self, old=300.0, new=200.0):
        scheduled = self.track("Scheduled", old)
        requested = self.track("Requested", new)
        item = self.queue_item(scheduled)
        req = self.make_request(requested, submitted_at=timezone.now() - timedelta(hours=1))
        return scheduled, requested, item, req

    def test_committed_shorter_swap_offers_one_evidence_after_commit(self):
        scheduled, requested, item, req = self.fixture()
        result = maybe_schedule_song_request(item, refill_evidence_sink=self.sink)
        self.assertEqual(result.track_id, requested.id)
        (evidence,) = self.offers
        fields = evidence.cause_fields()
        self.assertEqual(fields["source_key"],
                         f"request_substitution:li{item.id}:req{req.id}:t{scheduled.id}-{requested.id}")
        self.assertEqual((fields["original_effective_airtime_seconds"],
                          fields["replacement_effective_airtime_seconds"],
                          fields["observed_shortening_seconds"]), (300.0, 200.0, 100.0))
        self.assertEqual((fields["scheduled_category_code"], fields["scheduled_category_kind_code"]),
                         ("TESTMUS", "music"))
        self.assertEqual(fields["original_track_title"], "Scheduled")
        self.assertEqual(HourRefillCause.objects.count(), 0)  # the swap itself wrote nothing

    def test_sink_adds_no_sql_savepoint_or_phase_a_table_access(self):
        def run(sink):
            SongRequest.objects.all().delete()
            LogItem.objects.all().delete()
            _s, _r, item, _req = self.fixture()
            with CaptureQueriesContext(connection) as queries:
                maybe_schedule_song_request(item, **({"refill_evidence_sink": sink} if sink else {}))
            return [q["sql"] for q in queries.captured_queries]

        run(None)  # warm-up: one-time config get_or_create
        without = run(None)
        with_sink = run(self.sink)
        self.assertEqual(len(self.offers), 1)
        self.assertEqual(len(with_sink), len(without))
        joined = "\n".join(with_sink).lower()
        for forbidden in ("savepoint", "library_hourrefillpolicy", "library_hourrefillcause"):
            self.assertNotIn(forbidden, joined)

    def test_rolled_back_swap_never_reaches_the_sink(self):
        _scheduled, requested, item, _req = self.fixture()
        self.make_request(requested, submitted_at=timezone.now() - timedelta(minutes=1))  # collapse work
        real = services_module.estimate_air_time
        calls = []

        def fail_second(log_item):
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("injected collapse failure")
            return real(log_item)

        with patch.object(services_module, "estimate_air_time", side_effect=fail_second):
            maybe_schedule_song_request(item, refill_evidence_sink=self.sink)
        self.assertEqual(self.offers, [])
        self.assertEqual(SongRequest.objects.filter(status="scheduled").count(), 0)

    def test_non_causes_offer_nothing(self):
        for label, (old, new) in {"equal": (300.0, 300.0), "longer": (200.0, 300.0)}.items():
            with self.subTest(label):
                SongRequest.objects.all().delete()
                LogItem.objects.all().delete()
                _s, requested, item, _req = self.fixture(old, new)
                result = maybe_schedule_song_request(item, refill_evidence_sink=self.sink)
                self.assertEqual(result.track_id, requested.id)
        self.assertEqual(self.offers, [])

    def test_stale_or_uncached_caller_track_fails_closed_and_still_swaps(self):
        def run(sink):
            SongRequest.objects.all().delete()
            LogItem.objects.all().delete()
            _s, requested, item, _req = self.fixture()
            uncached = LogItem.objects.get(pk=item.pk)  # no select_related: Track not in memory
            with CaptureQueriesContext(connection) as queries:
                result = maybe_schedule_song_request(uncached, **({"refill_evidence_sink": sink} if sink else {}))
            self.assertEqual(result.track_id, requested.id)
            return len(queries.captured_queries)

        run(None)  # warm-up: one-time config get_or_create
        self.assertEqual(run(self.sink), run(None))  # no lazy Track load to build evidence
        self.assertEqual(self.offers, [])

        SongRequest.objects.all().delete()
        LogItem.objects.all().delete()
        _s, requested, item, _req = self.fixture()
        stale_track = self.track("Something Else", 500.0)
        item.track = stale_track  # the engine's copy no longer matches the locked row
        maybe_schedule_song_request(item, refill_evidence_sink=self.sink)
        self.assertEqual(self.offers, [])

    def test_a_failing_sink_cannot_undo_the_swap(self):
        _scheduled, requested, item, req = self.fixture()

        def broken(evidence):
            raise RuntimeError("sink bug")

        result = maybe_schedule_song_request(item, refill_evidence_sink=broken)
        self.assertEqual(result.track_id, requested.id)
        req.refresh_from_db()
        self.assertEqual((req.status, req.log_item_id), ("scheduled", item.id))

    def test_swap_preserves_the_occurrence_origin(self):
        _scheduled, _requested, item, _req = self.fixture()
        maybe_schedule_song_request(item, refill_evidence_sink=self.sink)
        item.refresh_from_db()
        self.assertEqual(item.origin, LogItem.ORIGIN_GENERATED_SCHEDULE)

    def test_end_to_end_with_the_real_worker_and_reconciler_key_deduplication(self):
        HourRefillPolicy(enabled=True, capture_request_substitution=True).save()
        recorder = HourRefillEvidenceRecorder()
        self.addCleanup(recorder.stop, 2.0)
        recorder.start()
        self.assertTrue(recorder.wait_until_ready(5))
        self.assertTrue(recorder.capture_requests)
        scheduled, requested, item, req = self.fixture()
        maybe_schedule_song_request(item, refill_evidence_sink=recorder.offer)
        deadline = time.monotonic() + 5
        while recorder.counters["recorded"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        cause = HourRefillCause.objects.get()
        self.assertEqual((cause.log_item_id_snapshot, cause.song_request_id_snapshot,
                          cause.original_track_id_snapshot, cause.replacement_track_id_snapshot),
                         (item.id, req.id, scheduled.id, requested.id))
        # The reconciler derives the identical key; a second record is a duplicate.
        recorder.offer(_same_key(cause))
        deadline = time.monotonic() + 5
        while recorder.counters["duplicate"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(HourRefillCause.objects.count(), 1)


def _same_key(cause):
    from library.services.hour_refill_causality import build_request_substitution_evidence

    evidence, _ = build_request_substitution_evidence(
        occurred_at=cause.occurred_at, playlist_log_id=cause.playlist_log_id_snapshot,
        log_date=cause.log_date, log_hour=cause.log_hour, log_item_id=cause.log_item_id_snapshot,
        log_item_position=cause.log_item_position, scheduled_category_id=cause.scheduled_category_id_snapshot,
        scheduled_category_code=cause.scheduled_category_code, scheduled_category_kind_code="music",
        original_track_id=cause.original_track_id_snapshot, original_track_title="",
        original_track_artist="", original_effective_airtime_seconds=cause.original_effective_airtime_seconds,
        replacement_track_id=cause.replacement_track_id_snapshot,
        replacement_effective_airtime_seconds=cause.replacement_effective_airtime_seconds,
        song_request_id=cause.song_request_id_snapshot,
    )
    return evidence
