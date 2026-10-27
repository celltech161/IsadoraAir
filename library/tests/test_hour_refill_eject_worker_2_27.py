"""Roadmap 2.27 Phase A: completed-Eject evidence and the evidence worker.

The Eject tests drive the real PlaybackEngine._eject_deck on a bare
stand-in (no GStreamer) with real, queue-loaded LogItems, and run every
Eject under a database blocker: the engine side of capture must issue zero
queries. The worker tests run the real thread against the test database."""
import threading
import time
from datetime import date, datetime, timezone as dt_timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.models import (
    Artist, Category, CategoryKind, HourRefillCause, HourRefillPolicy, LogItem, PlaylistLog, Track,
)
from library.services.hour_refill_causality import (
    EjectCauseEnvelope,
    EjectCauseEvidence,
    build_request_substitution_evidence,
)
from library.services.hour_refill_evidence import HourRefillEvidenceRecorder

REPO = Path(__file__).resolve().parents[2]
QUEUE_RELATED = ("track", "track__artist", "track__category", "track__category__kind", "category", "category__kind")


class _NoQueries:
    def __init__(self):
        self.calls = []

    def __call__(self, execute, sql, params, many, context):
        self.calls.append(sql)
        raise AssertionError(f"engine-thread evidence path issued SQL: {sql}")


class FakeRecorder:
    def __init__(self, capture_eject=True):
        self.capture_eject = capture_eject
        self.capture_requests = False
        self.offers = []
        self.snapshot_errors = 0

    def offer(self, evidence):
        self.offers.append(evidence)
        return True

    def note_snapshot_error(self):
        self.snapshot_errors += 1


class FakeDeck:
    """The Deck attributes _eject_deck and the snapshot read."""

    def __init__(self, slot, log_item, *, generation=17, occurrence_order=1, **extra):
        self.slot = slot
        self.log_item = log_item
        self.track = log_item.track
        self.generation = generation
        self.occurrence_order = occurrence_order
        self.paused = False
        self.finished = False
        self.retirement_started = False
        self.continuation_reason = None
        self.air_start_recorded = True
        self.completion_claimed = False
        self.completion_reason = None
        self.__dict__.update(extra)

    def claim_completion(self, reason):
        if self.completion_claimed:
            return False
        self.completion_claimed, self.completion_reason = True, reason
        return True


class EjectEvidenceHookTests(TestCase):
    def setUp(self):
        music, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        legal, _ = CategoryKind.objects.get_or_create(code="legal_id", defaults={"name": "Legal ID"})
        self.gold = Category.objects.create(code="TGOLD", name="Gold", kind=music)
        self.legal = Category.objects.create(code="TLEGAL", name="Legal", kind=legal)
        artist = Artist.objects.create(name="Eject Artist")
        self.log = PlaylistLog.objects.create(date=date(2027, 6, 1), hour=14, status="approved")
        for i, (origin, category) in enumerate([
            (LogItem.ORIGIN_GENERATED_SCHEDULE, self.gold),
            (LogItem.ORIGIN_GENERATED_SCHEDULE, self.gold),
            (LogItem.ORIGIN_OPERATOR_INSERTED, self.gold),
            (LogItem.ORIGIN_LIVE_FILL, self.gold),
            (LogItem.ORIGIN_ENGINE_INSERTED, self.gold),
            (LogItem.ORIGIN_GENERATED_FILL, self.gold),
            (None, self.gold),
            (LogItem.ORIGIN_GENERATED_SCHEDULE, self.legal),
        ]):
            track = Track.objects.create(
                title=f"Song {i}", artist=artist, category=self.gold, filepath=f"/nonexistent/{i}.mp3",
                duration_seconds=260.0, cue_in_seconds=2.0, next_start_seconds=250.0,
            )
            LogItem.objects.create(
                playlist_log=self.log, position=i, scheduled_time=timezone.now(), track=track,
                track_title=track.title, track_artist=artist.name, category=category, origin=origin,
            )
        # Loaded exactly as the engine's queue loads them.
        self.items = list(self.log.items.select_related(*QUEUE_RELATED).order_by("position"))

    def engine(self, recorder=None, **state):
        engine = object.__new__(eng_module.PlaybackEngine)
        engine._lock = threading.RLock()
        engine.decks = {"A": None, "B": None}
        engine._vt = {"phase": "idle"}
        engine._start_next_active = None
        engine._next_triggered = False
        engine.manual_mode = False
        engine.mic_live = False
        engine.remote_dj_session = None
        engine.dj_slots = []
        engine._engine_session_id = "engine-instance-1"
        engine._hour_refill_evidence = recorder if recorder is not None else FakeRecorder()
        engine._start_next_cancel_for_operator_command = MagicMock()
        engine._retire_deck_respecting_gated_seek = MagicMock()
        engine._start_next_track = MagicMock()
        engine._get_deck_position = MagicMock(return_value=100.0)
        for name, value in state.items():
            setattr(engine, name, value)
        return engine

    def eject(self, engine, slot="A", expected=None):
        blocker = _NoQueries()
        with connection.execute_wrapper(blocker):
            engine._eject_deck(slot, expected_log_item_id=expected)
        self.assertEqual(blocker.calls, [])
        return engine._hour_refill_evidence

    def single(self, item_index=0, recorder=None, deck_extra=None, **state):
        engine = self.engine(recorder, **state)
        deck = FakeDeck("A", self.items[item_index], **(deck_extra or {}))
        engine.decks["A"] = deck
        return engine, deck

    # -- the eligible case -----------------------------------------------------

    def test_completed_single_deck_eject_of_generated_music_is_one_cause(self):
        engine, deck = self.single()
        recorder = self.eject(engine, expected=self.items[0].id)
        (evidence,) = recorder.offers
        self.assertIsInstance(evidence, EjectCauseEvidence)
        fields = evidence.cause_fields()
        self.assertEqual(fields["cause_type"], "operator_eject")
        self.assertEqual(fields["source_key"], f"operator_eject:engine-instance-1:A:g17:li{self.items[0].id}")
        self.assertEqual(fields["observed_shortening_seconds"], 150.0)  # 250 - 100, not file length
        self.assertEqual(fields["original_effective_airtime_seconds"], 248.0)
        self.assertEqual(fields["eject_position_seconds"], 100.0)
        self.assertEqual((fields["log_date"], fields["log_hour"], fields["log_item_position"]), (date(2027, 6, 1), 14, 0))
        self.assertEqual((fields["scheduled_category_code"], fields["scheduled_category_kind_code"]), ("TGOLD", "music"))
        self.assertEqual((fields["deck_slot"], fields["deck_generation"]), ("A", 17))
        # The Eject itself proceeded exactly as before.
        engine._retire_deck_respecting_gated_seek.assert_called_once()
        self.assertEqual(deck.completion_reason, "eject")

    def test_capture_off_does_nothing_and_does_not_read_position(self):
        engine, _deck = self.single(recorder=FakeRecorder(capture_eject=False))
        recorder = self.eject(engine)
        self.assertEqual(recorder.offers, [])
        engine._get_deck_position.assert_not_called()
        engine._retire_deck_respecting_gated_seek.assert_called_once()

    def test_no_recorder_attribute_is_harmless(self):
        engine, _deck = self.single()
        del engine._hour_refill_evidence
        engine._eject_deck("A")
        engine._retire_deck_respecting_gated_seek.assert_called_once()

    # -- exclusions --------------------------------------------------------------

    def assert_no_cause(self, engine, slot="A", expected=None):
        recorder = self.eject(engine, slot=slot, expected=expected)
        self.assertEqual(recorder.offers, [])
        self.assertEqual(recorder.snapshot_errors, 0)
        return recorder

    def test_crossfade_either_deck_is_not_a_cause(self):
        for target in ("outgoing", "incoming"):
            with self.subTest(target):
                engine = self.engine()
                engine.decks["A"] = FakeDeck("A", self.items[0], generation=10, occurrence_order=1)
                engine.decks["B"] = FakeDeck("B", self.items[1], generation=11, occurrence_order=2)
                self.assert_no_cause(engine, slot="A" if target == "outgoing" else "B")
                engine._retire_deck_respecting_gated_seek.assert_called_once()

    def test_engine_state_exclusions(self):
        cases = {
            "start next transition": dict(_start_next_active={"request_id": "r1"}),
            "transition pending": dict(_next_triggered=True),
            "manual": dict(manual_mode=True),
            "local mic live": dict(mic_live=True),
            "remote dj session": dict(remote_dj_session=SimpleNamespace(slot_id=None)),
            "voice track": dict(_vt={"phase": "outro_playing", "outgoing_track_id": -1}),
        }
        for label, state in cases.items():
            with self.subTest(label):
                engine, _deck = self.single(**state)
                self.assert_no_cause(engine)

    def test_deck_state_exclusions(self):
        cases = {
            "paused": dict(paused=True),
            "not yet on air": dict(air_start_recorded=False),
            "continuation (resume or seek recreate)": dict(continuation_reason="manual_seek"),
        }
        for label, extra in cases.items():
            with self.subTest(label):
                engine, _deck = self.single(deck_extra=extra)
                self.assert_no_cause(engine)

    def test_only_generated_schedule_origin_qualifies(self):
        for index, label in [(2, "operator inserted"), (3, "live fill"), (4, "engine inserted"),
                             (5, "build-time fill"), (6, "unknown/legacy")]:
            with self.subTest(label):
                engine, _deck = self.single(item_index=index)
                self.assert_no_cause(engine)

    def test_non_music_slot_is_not_a_cause(self):
        engine, _deck = self.single(item_index=7)
        self.assert_no_cause(engine)

    def test_no_remaining_airtime_is_not_a_cause(self):
        engine, _deck = self.single(_get_deck_position=MagicMock(return_value=250.0))
        self.assert_no_cause(engine)

    def test_stale_empty_and_repeated_ejects_are_not_causes(self):
        engine, _deck = self.single()
        self.assert_no_cause(engine, expected=self.items[1].id)              # stale binding
        engine._retire_deck_respecting_gated_seek.assert_not_called()
        engine, deck = self.single(deck_extra=dict(completion_claimed=True, completion_reason="eos"))
        self.assert_no_cause(engine)                                          # already completing
        engine._retire_deck_respecting_gated_seek.assert_not_called()
        engine = self.engine()
        self.assert_no_cause(engine)                                          # empty slot
        engine._start_next_track.assert_called_once()

    def test_resumed_item_without_cached_category_fails_closed_without_a_query(self):
        resumed = LogItem.objects.select_related("playlist_log", "track").get(pk=self.items[0].pk)
        engine = self.engine()
        engine.decks["A"] = FakeDeck("A", resumed)
        self.assert_no_cause(engine)

    def test_snapshot_failure_never_affects_the_eject(self):
        engine, deck = self.single(_get_deck_position=MagicMock(side_effect=RuntimeError("gst")))
        recorder = self.eject(engine)
        self.assertEqual(recorder.offers, [])
        self.assertEqual(recorder.snapshot_errors, 1)
        engine._retire_deck_respecting_gated_seek.assert_called_once()
        self.assertEqual(deck.completion_reason, "eject")


def _eject_evidence(n=1, generation=17):
    return EjectCauseEvidence(
        envelope=EjectCauseEnvelope(
            source_key=f"operator_eject:eng:A:g{generation}:li{n}", engine_instance="eng", slot="A",
            deck_generation=generation, log_item_id=n, eject_position_seconds=100.0,
            remaining_effective_airtime_seconds=150.0,
        ),
        occurred_at=datetime(2027, 6, 1, 14, 5, tzinfo=dt_timezone.utc), playlist_log_id=1,
        log_date=date(2027, 6, 1), log_hour=14, log_item_position=n, scheduled_category_id=3,
        scheduled_category_code="TGOLD", scheduled_category_kind_code="music", original_track_id=9,
        original_track_title="Song", original_track_artist="Artist", original_effective_airtime_seconds=248.0,
    )


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class EvidenceWorkerTests(TransactionTestCase):
    def recorder(self, **kwargs):
        recorder = HourRefillEvidenceRecorder(**kwargs)
        self.addCleanup(recorder.stop, 2.0)
        return recorder

    def enable(self, eject=True, requests=True):
        HourRefillPolicy(enabled=True, capture_operator_eject=eject, capture_request_substitution=requests).save()

    def test_worker_alone_writes_and_engine_side_offer_issues_no_sql(self):
        self.enable()
        recorder = self.recorder()
        recorder.start()
        self.assertTrue(recorder.wait_until_ready(5))
        self.assertTrue(recorder.capture_eject)
        blocker = _NoQueries()
        with connection.execute_wrapper(blocker):
            self.assertTrue(recorder.offer(_eject_evidence()))
        self.assertTrue(_wait(lambda: recorder.counters["recorded"] == 1))
        row = HourRefillCause.objects.get()
        self.assertEqual((row.cause_type, row.observed_shortening_seconds, row.deck_generation),
                         ("operator_eject", 150.0, 17))

    def test_request_evidence_is_written_by_the_same_worker(self):
        self.enable()
        recorder = self.recorder()
        recorder.start()
        recorder.wait_until_ready(5)
        evidence, _ = build_request_substitution_evidence(
            occurred_at=timezone.now(), playlist_log_id=1, log_date=date(2027, 6, 1), log_hour=14,
            log_item_id=5, log_item_position=5, scheduled_category_id=3, scheduled_category_code="TGOLD",
            scheduled_category_kind_code="music", original_track_id=1, original_track_title="A",
            original_track_artist="B", original_effective_airtime_seconds=300.0, replacement_track_id=2,
            replacement_effective_airtime_seconds=200.0, song_request_id=7,
        )
        recorder.offer(evidence)
        self.assertTrue(_wait(lambda: recorder.counters["recorded"] == 1))
        self.assertEqual(HourRefillCause.objects.get().song_request_id_snapshot, 7)

    def test_duplicate_source_key_is_suppressed(self):
        self.enable()
        recorder = self.recorder()
        recorder.start()
        recorder.wait_until_ready(5)
        recorder.offer(_eject_evidence())
        recorder.offer(_eject_evidence())
        self.assertTrue(_wait(lambda: recorder.counters["recorded"] + recorder.counters["duplicate"] == 2))
        self.assertEqual((recorder.counters["recorded"], recorder.counters["duplicate"]), (1, 1))
        self.assertEqual(HourRefillCause.objects.count(), 1)

    def test_policy_off_or_unreadable_fails_closed(self):
        recorder = self.recorder()
        recorder.start()
        recorder.wait_until_ready(5)
        self.assertEqual((recorder.capture_requests, recorder.capture_eject), (False, False))
        recorder.offer(_eject_evidence())  # an event offered anyway is still refused worker-side
        self.assertTrue(_wait(lambda: recorder.counters["dropped_policy_off"] == 1))
        self.assertEqual(HourRefillCause.objects.count(), 0)

        broken = self.recorder(read_flags=MagicMock(side_effect=RuntimeError("db down")))
        broken.start()
        broken.wait_until_ready(5)
        self.assertEqual((broken.capture_requests, broken.capture_eject), (False, False))

    def test_eject_only_policy_does_not_record_requests(self):
        self.enable(eject=True, requests=False)
        recorder = self.recorder()
        recorder.start()
        recorder.wait_until_ready(5)
        self.assertEqual((recorder.capture_requests, recorder.capture_eject), (False, True))

    def test_not_running_drops_without_waiting(self):
        recorder = self.recorder()
        with patch("builtins.print") as output:
            self.assertFalse(recorder.offer(_eject_evidence()))
            recorder.note_snapshot_error()
        output.assert_not_called()  # both methods may run on the playback thread
        self.assertEqual(recorder.counters["dropped_not_running"], 1)
        self.assertEqual(recorder.counters["snapshot_error"], 1)

    def test_queue_full_drops_immediately(self):
        self.enable()
        release = threading.Event()
        entered = threading.Event()

        def slow_insert(evidence):
            entered.set()
            release.wait(5)

        recorder = self.recorder(max_queued=1, insert=slow_insert)
        self.addCleanup(release.set)
        recorder.start()
        recorder.wait_until_ready(5)
        recorder.offer(_eject_evidence(1))
        self.assertTrue(entered.wait(5))      # worker busy with #1
        self.assertTrue(recorder.offer(_eject_evidence(2)))   # fills the queue
        started = time.monotonic()
        self.assertFalse(recorder.offer(_eject_evidence(3)))  # full: dropped, not waited for
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(recorder.counters["dropped_full"], 1)

    def test_insert_failure_is_counted_and_the_worker_continues(self):
        self.enable()
        calls = []

        def flaky_insert(evidence):
            calls.append(evidence)
            if len(calls) == 1:
                raise RuntimeError("injected DB failure")
            HourRefillCause.objects.create(**evidence.cause_fields())

        recorder = self.recorder(insert=flaky_insert)
        recorder.start()
        recorder.wait_until_ready(5)
        recorder.offer(_eject_evidence(1))
        recorder.offer(_eject_evidence(2))
        self.assertTrue(_wait(lambda: recorder.counters["recorded"] == 1 and recorder.counters["dropped_error"] == 1))
        self.assertEqual(HourRefillCause.objects.get().log_item_id_snapshot, 2)

    def test_orderly_stop_drains_queued_events(self):
        self.enable()
        gate = threading.Event()

        def gated_insert(evidence):
            gate.wait(5)
            HourRefillCause.objects.create(**evidence.cause_fields())

        recorder = self.recorder(insert=gated_insert)
        recorder.start()
        recorder.wait_until_ready(5)
        for n in (1, 2, 3):
            recorder.offer(_eject_evidence(n))
        gate.set()
        recorder.stop(drain_seconds=5.0)
        self.assertEqual(HourRefillCause.objects.count(), 3)
        self.assertFalse(recorder.offer(_eject_evidence(4)))
        self.assertEqual((recorder.capture_requests, recorder.capture_eject), (False, False))

    def test_stop_is_bounded_when_the_worker_is_stuck(self):
        self.enable()
        release = threading.Event()
        entered = threading.Event()
        inserted = []

        def stuck_insert(evidence):
            inserted.append(evidence.source_key)
            entered.set()
            release.wait(10)

        recorder = self.recorder(insert=stuck_insert)
        self.addCleanup(release.set)
        recorder.start()
        recorder.wait_until_ready(5)
        recorder.offer(_eject_evidence(1))
        self.assertTrue(entered.wait(5))
        recorder.offer(_eject_evidence(2))
        started = time.monotonic()
        recorder.stop(drain_seconds=0.2)
        self.assertLess(time.monotonic() - started, 1.0)
        release.set()
        recorder._thread.join(2.0)
        self.assertFalse(recorder._thread.is_alive())
        self.assertEqual(inserted, [_eject_evidence(1).source_key])
        self.assertEqual(recorder.counters["dropped_shutdown"], 1)

    def test_start_is_idempotent(self):
        recorder = self.recorder()
        recorder.start()
        first = recorder._thread
        recorder.start()
        self.assertIs(recorder._thread, first)
        self.assertEqual(
            [t for t in threading.enumerate() if t.name == "hour-refill-evidence" and t.is_alive()].count(first), 1,
        )


class EngineLifecycleStructureTests(SimpleTestCase):
    """PlaybackEngine can't be constructed without GStreamer; pin the wiring."""

    def test_one_recorder_started_with_the_engine_and_stopped_after_audio_is_null(self):
        source = (REPO / "library/services/engine.py").read_text()
        self.assertEqual(source.count("HourRefillEvidenceRecorder()"), 1)
        self.assertEqual(source.count("evidence.start()"), 1)
        stop_body = source[source.index("    def stop(self):"):source.index("    def _handle_signal_glib(self):")]
        self.assertLess(stop_body.index("self.main_pipeline.set_state(Gst.State.NULL)"),
                        stop_body.index("evidence.stop()"))
