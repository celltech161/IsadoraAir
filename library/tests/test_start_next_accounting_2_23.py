"""Roadmap 2.23 -- Start Next and Eject against the REAL occurrence ledger.

The transport harness's real GStreamer decks with the production claim,
air-start and duration-segment persistence restored, over real
LogItem/PlayEvent rows: exactly the occurrences that aired are claimed and
counted, nothing else is consumed, and each retirement's closing segment
carries its truthful termination reason.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from library.models import LogItem, PlayEvent, PlayEventSegment
from library.tests._transport_harness import (
    TransportHarness,
    pump,
    start_next_command,
    wait_until,
    write_tone_wav,
)
from library.tests.test_engine_playback_accounting_semantics import PlaybackAccountingFixture
from library.tests.cue_marker_support import set_markers


class StartNextLedgerTests(PlaybackAccountingFixture):
    def setUp(self):
        super().setUp()
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        for name in ("_claim_playback_occurrence", "_schedule_occurrence_air_start_from_probe",
                     "_persist_deck_duration", "_schedule_continuation_segment_start"):
            self.engine.__dict__.pop(name)

    def occurrences(self, count, seconds=8.0):
        items = []
        for _ in range(count):
            track, item = self.make_occurrence()
            path = Path(self.h.root) / f"ledger-{uuid.uuid4().hex}.wav"
            write_tone_wav(path, seconds=seconds)
            track.filepath = str(path)
            track.filename = path.name
            track.duration_seconds = seconds
            track.save(update_fields=["filepath", "filename", "duration_seconds"])
            set_markers(track, next_start_seconds=seconds, cue_in_seconds=0.0)
            item.refresh_from_db()
            items.append(item)
        self.engine.log_items = items
        self.engine._queue_cursor = 0
        self.engine.current_log = self.log
        return items

    def aired(self, item):
        return PlayEvent.objects.filter(log_item_id_snapshot=item.id).exists()

    def settle(self):
        self.assertTrue(wait_until(lambda: self.engine._start_next_active is None, 8.0))
        return self.engine._start_next_state()["recent"][0]

    def segments(self, item):
        return list(PlayEventSegment.objects.filter(log_item_id_snapshot=item.id)
                    .order_by("started_at"))

    def test_start_next_airs_exactly_one_next_occurrence_with_truthful_evidence(self):
        one, two, three = self.occurrences(3)
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.aired(one), 5.0))
        self.engine._dispatch_engine_command(start_next_command(self.engine, fade_ms=400))
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertTrue(wait_until(lambda: self.aired(two), 5.0))
        pump(0.5)
        for item in (one, two, three):
            item.refresh_from_db()
        self.assertIsNotNone(one.played_at)
        self.assertIsNotNone(two.played_at)
        self.assertIsNone(three.playback_claimed_at, "a third occurrence was consumed")
        self.assertEqual(PlayEvent.objects.count(), 2)
        closing = self.segments(one)[-1]
        self.assertEqual(closing.termination_reason, "start_next")
        self.assertEqual(closing.evidence_state, "complete")
        self.assertIsNotNone(closing.ended_at)
        self.assertEqual(self.segments(two)[-1].evidence_state, "active")

    def test_fade_to_silence_consumes_nothing_and_closes_the_faded_occurrence(self):
        (one,) = self.occurrences(1)
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.aired(one), 5.0))
        self.engine._dispatch_engine_command(start_next_command(self.engine, fade_ms=400))
        self.assertEqual(self.settle()["outcome"], "completed_to_silence")
        pump(0.5)
        self.assertEqual(PlayEvent.objects.count(), 1)
        self.assertEqual(LogItem.objects.filter(playback_claimed_at__isnull=False).count(), 1)
        self.assertEqual(self.segments(one)[-1].termination_reason, "start_next")

    def test_overlap_start_next_claims_no_third_occurrence(self):
        one, two, three = self.occurrences(3)
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.aired(one), 5.0))
        self.engine._start_next_track(slot="B")
        self.assertTrue(wait_until(lambda: self.aired(two), 5.0))
        self.engine._dispatch_engine_command(start_next_command(self.engine, fade_ms=400))
        self.assertEqual(self.settle()["outcome"], "completed")
        pump(0.5)
        three.refresh_from_db()
        self.assertIsNone(three.playback_claimed_at)
        self.assertEqual(PlayEvent.objects.count(), 2)
        self.assertEqual(self.segments(one)[-1].termination_reason, "start_next")

    def test_eject_of_the_outgoing_deck_in_a_crossfade_records_eject_and_claims_nothing_more(self):
        one, two, three = self.occurrences(3)
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.aired(one), 5.0))
        self.engine._start_next_track(slot="B")
        self.assertTrue(wait_until(lambda: self.aired(two), 5.0))
        self.engine._eject_deck("A")
        pump(0.8)
        three.refresh_from_db()
        self.assertIsNone(three.playback_claimed_at)
        self.assertEqual(self.segments(one)[-1].termination_reason, "eject")
        self.assertEqual(PlayEvent.objects.count(), 2)
