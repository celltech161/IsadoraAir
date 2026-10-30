"""Roadmap 2.27, r0121 -- automatic skip and media failure as hour-refill causes.

Contract: docs/roadmap/2_27_AUTOMATIC_FAILURE_CAUSALITY_EXTENSION.md.

- Classification: pure decision tables on plain values; the engine's real
  playability reasons and GStreamer's real error codes are pinned.
- Skip hook: the real _peek_playable_at_cursor / _next_queue_item /
  _get_upcoming_preview on a bare stand-in with real, queue-loaded rows,
  under a database blocker (the engine side issues zero queries).
- Failure hook: the real _on_deck_error on a stand-in, then on real
  GStreamer decks (a genuinely undecodable file; a real element error after
  air; a generic flow error; an ordinary EOS).
- Worker, schema, projection, policy and installer against the guarded
  test database.
"""
import os
import threading
import time
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import IntegrityError, connection, transaction  # noqa: E402
from django.db.migrations.loader import MigrationLoader  # noqa: E402
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402

import library.services.engine as eng_module  # noqa: E402
from library.models import (  # noqa: E402
    Artist, Category, CategoryKind, HourRefillCause, HourRefillInstall, HourRefillPolicy, LogItem,
    PlaylistLog, Track,
)
from library.services import hour_refill_install as inst  # noqa: E402
from library.services import hour_refill_projection as proj  # noqa: E402
from library.services import hour_refill_runner as runner_mod  # noqa: E402
from library.services.hour_refill_causality import (  # noqa: E402
    CAUSE_AUTOMATIC_SKIP,
    CAUSE_MEDIA_FAILURE,
    CAUSE_OPERATOR_EJECT,
    CAUSE_REQUEST_SUBSTITUTION,
    CAUSE_START_NEXT,
    MEDIA_ERROR_CODES,
    PHASE_POST_AIR,
    PHASE_PRE_AIR,
    AutomaticSkipCauseEvidence,
    AutomaticSkipObservation,
    CauseRecord,
    MediaFailureCauseEvidence,
    MediaFailureObservation,
    ScheduledOccurrence,
    automatic_skip_source_key,
    build_causality_trace,
    classify_automatic_skip,
    classify_media_failure,
    media_error_code,
    media_failure_source_key,
    skip_reason_code,
)
from library.services.hour_refill_evidence import HourRefillEvidenceRecorder  # noqa: E402
from library.tests.test_hour_refill_eject_worker_2_27 import QUEUE_RELATED, _NoQueries, _wait  # noqa: E402
from library.tests.test_hour_refill_install_2_27 import InstallFixtures  # noqa: E402
from library.tests.test_hour_refill_projection_2_27 import (  # noqa: E402
    GOLD, LOG, at, boundary, deck as deck_state, eject, item, snapshot, swap,
)
from library.tests.test_hour_refill_proposal_2_27 import at as refill_at  # noqa: E402
from library.tests.test_hour_refill_start_next_2_27 import cut as start_next_cut  # noqa: E402
from updatecenter.management.commands.updatecenter_probe import _serialize_exact_plan  # noqa: E402

ENGINE = "engine-instance-1"
ARMED = override_settings(HOUR_REFILL_INSTALL_ARMED=True)
DAY = date(2027, 6, 1)


def occurrence(**overrides):
    values = dict(
        origin_source="scheduled", kind_code="music", log_approved=True, played=False, claimed=False,
        log_item_id=41, playlist_log_id=7, log_date=DAY, log_hour=14, position=3, category_id=5,
        category_code="GOLD", track_id=501, track_title="Song", track_artist="Artist",
        cue_in_seconds=2.0, next_start_seconds=250.0, duration_seconds=260.0, complete=True,
    )
    values.update(overrides)
    return ScheduledOccurrence(**values)


def failure(**overrides):
    values = dict(
        occurrence=occurrence(claimed=True), engine_instance=ENGINE, slot="A", deck_generation=17,
        error_code="stream_decode", completion_claimed=True, deck_mapped=True, fresh=True,
    )
    values.update(overrides)
    return MediaFailureObservation(**values)


def post_air(**overrides):
    values = dict(occurrence=occurrence(claimed=True, played=True), air_start_dispatched=True,
                  air_start_recorded=True, media_buffers=40, position_seconds=100.0)
    values.update(overrides)
    return failure(**values)


# ---------------------------------------------------------------------------
# 1. Pure classification
# ---------------------------------------------------------------------------

class AutomaticClassificationTests(SimpleTestCase):
    def test_only_the_engines_two_media_reasons_are_skip_evidence(self):
        self.assertEqual(skip_reason_code("file missing on disk: /x/y.mp3"), "file_missing")
        self.assertEqual(skip_reason_code("track id=12 has no filepath"), "no_filepath")
        for other in ("track deleted from library (FK set NULL)", "spoken projection authority unavailable: x",
                      "active message refused", "", None, 3):
            with self.subTest(other):
                self.assertIsNone(skip_reason_code(other))

    def test_media_error_codes_are_gstreamers_own_and_exclude_flow_and_seek(self):
        Gst.init(None)
        resource, stream = Gst.ResourceError.quark(), Gst.StreamError.quark()
        expected = {
            (GLib.quark_to_string(resource), int(Gst.ResourceError.NOT_FOUND)): "resource_not_found",
            (GLib.quark_to_string(resource), int(Gst.ResourceError.OPEN_READ)): "resource_open_read",
            (GLib.quark_to_string(resource), int(Gst.ResourceError.READ)): "resource_read",
            (GLib.quark_to_string(stream), int(Gst.StreamError.TYPE_NOT_FOUND)): "stream_type_not_found",
            (GLib.quark_to_string(stream), int(Gst.StreamError.WRONG_TYPE)): "stream_wrong_type",
            (GLib.quark_to_string(stream), int(Gst.StreamError.CODEC_NOT_FOUND)): "stream_codec_not_found",
            (GLib.quark_to_string(stream), int(Gst.StreamError.DECODE)): "stream_decode",
            (GLib.quark_to_string(stream), int(Gst.StreamError.DEMUX)): "stream_demux",
            (GLib.quark_to_string(stream), int(Gst.StreamError.FORMAT)): "stream_format",
        }
        self.assertEqual(MEDIA_ERROR_CODES, expected)
        for domain, code in [(GLib.quark_to_string(stream), int(Gst.StreamError.FAILED)),
                             (GLib.quark_to_string(resource), int(Gst.ResourceError.SEEK)),
                             (GLib.quark_to_string(resource), int(Gst.ResourceError.FAILED)),
                             (GLib.quark_to_string(Gst.CoreError.quark()), int(Gst.CoreError.FAILED)),
                             (None, 7), ("gst-stream-error-quark", True)]:
            with self.subTest(domain=domain, code=code):
                self.assertIsNone(media_error_code(domain, code))

    def test_a_committed_skip_loses_the_full_effective_airtime(self):
        lost, reason = classify_automatic_skip(AutomaticSkipObservation(occurrence(), "file_missing"))
        self.assertEqual((lost, reason), (248.0, None))                       # 250 - 2, not 260
        lost, _ = classify_automatic_skip(AutomaticSkipObservation(occurrence(next_start_seconds=None),
                                                                   "no_filepath"))
        self.assertEqual(lost, 258.0)

    def test_skip_exclusions(self):
        cases = {
            "not a media reason": (occurrence(), None, "not_media_reason"),
            "identity incomplete": (occurrence(complete=False), "file_missing", "identity_unknown"),
            "inserted": (occurrence(origin_source="operator_inserted"), "file_missing", "not_scheduled_occurrence"),
            "unknown origin": (occurrence(origin_source="unknown"), "file_missing", "not_scheduled_occurrence"),
            "not music": (occurrence(kind_code="spot"), "file_missing", "not_music"),
            "kind not loaded": (occurrence(kind_code=""), "file_missing", "not_music"),
            "unapproved log": (occurrence(log_approved=False), "file_missing", "log_not_approved"),
            "claimed": (occurrence(claimed=True), "file_missing", "possible_replay"),
            "aired": (occurrence(played=True), "file_missing", "possible_replay"),
            "zero airtime": (occurrence(next_start_seconds=2.0), "file_missing", "no_effective_airtime"),
        }
        for label, (occ, code, reason) in cases.items():
            with self.subTest(label):
                self.assertEqual(classify_automatic_skip(AutomaticSkipObservation(occ, code)), (None, reason))

    def test_pre_air_and_post_air_failures(self):
        self.assertEqual(classify_media_failure(failure()), ((PHASE_PRE_AIR, 248.0), None))
        self.assertEqual(classify_media_failure(post_air()), ((PHASE_POST_AIR, 150.0), None))
        self.assertEqual(classify_media_failure(post_air(position_seconds=1.0)), ((PHASE_POST_AIR, 248.0), None))

    def test_failure_exclusions_each_with_its_reason(self):
        cases = {
            "transport error": (failure(error_code=None), "not_media_error"),
            "not claimed": (failure(completion_claimed=False), "not_claimed"),
            "no engine": (failure(engine_instance=""), "identity_unknown"),
            "oversized engine": (failure(engine_instance="e" * 65), "identity_unknown"),
            "bad slot": (failure(slot="C"), "identity_unknown"),
            "no generation": (failure(deck_generation=None), "identity_unknown"),
            "superseded": (failure(deck_mapped=False), "superseded"),
            "gated seek": (failure(seek_in_progress=True), "seek_in_progress"),
            "continuation": (failure(fresh=False), "not_fresh_occurrence"),
            "start next": (failure(start_next_active=True), "start_next_transition"),
            "voice track": (failure(vt_phase="outro_playing"), "vt_transition"),
            "crossfade": (failure(other_live_deck=True), "overlap"),
            "manual": (failure(manual_mode=True), "manual_hold"),
            "remote dj": (failure(remote_dj_owner=True), "remote_dj_owner"),
            "mic": (failure(mic_live=True), "mic_live"),
            "inserted": (failure(occurrence=occurrence(origin_source="live_fill")), "not_scheduled_occurrence"),
            "not music": (failure(occurrence=occurrence(kind_code="legal_id")), "not_music"),
            "unapproved": (failure(occurrence=occurrence(log_approved=False)), "log_not_approved"),
            "identity incomplete": (failure(occurrence=occurrence(complete=False)), "identity_unknown"),
            "dispatched, not recorded": (failure(air_start_dispatched=True), "air_start_unsettled"),
            "buffers, no dispatch": (failure(media_buffers=3), "air_start_unsettled"),
            "pre-air but played_at set": (failure(occurrence=occurrence(played=True)), "air_start_unsettled"),
            "post-air position unknown": (post_air(position_seconds=None), "position_unknown"),
            "post-air position nan": (post_air(position_seconds=float("nan")), "position_unknown"),
            "post-air negative": (post_air(position_seconds=-1.0), "position_unknown"),
            "post-air at the end": (post_air(position_seconds=250.0), "no_remaining_airtime"),
            "post-air after the end": (post_air(position_seconds=255.0), "no_remaining_airtime"),
        }
        for label, (obs, reason) in cases.items():
            with self.subTest(label):
                self.assertEqual(classify_media_failure(obs), (None, reason))

    def test_source_keys_are_bounded_stable_and_private(self):
        self.assertEqual(automatic_skip_source_key(41, 501), "automatic_skip:li41:t501")
        key = media_failure_source_key(9_223_372_036_854_775_807, "B", 9_223_372_036_854_775_807, "e" * 64)
        self.assertLessEqual(len(key), 160)
        self.assertEqual(media_failure_source_key(41, "A", 17, ENGINE), f"media_failure:li41:Ag17:{ENGINE}")


# ---------------------------------------------------------------------------
# 2. Engine hooks on a stand-in (real methods, real rows, no SQL)
# ---------------------------------------------------------------------------

class AFRecorder:
    def __init__(self, skip=True, failure=True):
        self.capture_automatic_skip = skip
        self.capture_media_failure = failure
        self.capture_requests = self.capture_eject = self.capture_start_next = False
        self.offers = []
        self.snapshot_errors = 0

    def offer(self, evidence):
        self.offers.append(evidence)
        return True

    def note_snapshot_error(self):
        self.snapshot_errors += 1


class FailureDeck:
    """The Deck attributes _on_deck_error and the snapshot read."""

    def __init__(self, slot, log_item, *, generation=17, **extra):
        self.slot = slot
        self.log_item = log_item
        self.track = log_item.track
        self.generation = generation
        self.occurrence_order = 1
        self.paused = self.finished = self.retirement_started = False
        self.continuation_reason = None
        self.gated_seek = None
        self.air_start_eligible = True
        self.air_start_dispatched = False
        self.air_start_recorded = False
        self.media_buffer_count = 0
        self.observed_duration_seconds = 0.0
        self.completion_claimed = False
        self.completion_reason = None
        self.milestones = []
        self.__dict__.update(extra)

    def claim_completion(self, reason):
        if self.completion_claimed:
            return False
        self.completion_claimed, self.completion_reason = True, reason
        return True

    def mark_milestone(self, name, state=None):
        self.milestones.append(name)

    def aired(self, position_buffers=40):
        self.air_start_dispatched = self.air_start_recorded = True
        self.media_buffer_count = position_buffers
        return self


class ErrorMessage:
    def __init__(self, domain_quark, code, text="test media error"):
        self.error = GLib.Error.new_literal(domain_quark, text, int(code))

    def parse_error(self):
        return self.error, "debug detail"


def decode_error():
    Gst.init(None)
    return ErrorMessage(Gst.StreamError.quark(), Gst.StreamError.DECODE)


class HookFixtures:
    def build(self, tmp):
        music, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        legal, _ = CategoryKind.objects.get_or_create(code="legal_id", defaults={"name": "Legal ID"})
        self.gold = Category.objects.create(code="AFGOLD", name="Gold", kind=music)
        self.legal = Category.objects.create(code="AFLEGAL", name="Legal", kind=legal)
        self.artist = Artist.objects.create(name="Failure Artist")
        self.log = PlaylistLog.objects.create(date=DAY, hour=14, status="approved")
        self.present = os.path.join(tmp, "present.wav")
        open(self.present, "wb").close()
        self.n = 0

    def row(self, *, filepath="missing", origin=LogItem.ORIGIN_GENERATED_SCHEDULE, category=None, log=None,
            track=True, **extra):
        self.n += 1
        path = {"missing": f"/nonexistent/af-{self.n}.mp3", "present": None, "none": ""}.get(filepath, filepath)
        track_row = None
        if track:
            track_row = Track.objects.create(
                title=f"Song {self.n}", artist=self.artist, category=self.gold,
                filepath=path if path is not None else f"{self.present}.{self.n}.wav",
                duration_seconds=260.0, cue_in_seconds=2.0, next_start_seconds=250.0,
            )
            if path is None:
                open(track_row.filepath, "wb").close()
        return LogItem.objects.create(
            playlist_log=log or self.log, position=self.n, scheduled_time=timezone.now(), track=track_row,
            track_title=f"Song {self.n}", track_artist=self.artist.name, category=category or self.gold,
            origin=origin, **extra,
        )

    def loaded(self, log=None):
        return list((log or self.log).items.select_related(*QUEUE_RELATED).order_by("position"))

    def engine(self, recorder=None, items=None, **state):
        engine = object.__new__(eng_module.PlaybackEngine)
        engine._lock = threading.RLock()
        engine.decks = {"A": None, "B": None}
        engine.log_items = items if items is not None else []
        engine._queue_cursor = 0
        engine._forced_next_items = []
        engine._resume_item = None
        engine._resume_hint = None
        engine._poison_skip_identities = []
        engine._poison_reported_identities = set()
        engine.current_log = self.log
        engine._vt = {"phase": "idle"}
        engine._start_next_active = None
        engine._next_triggered = False
        engine.manual_mode = False
        engine.mic_live = False
        engine.remote_dj_session = None
        engine.dj_slots = []
        engine._engine_session_id = ENGINE
        engine._deck_bin_map = {}
        engine._runtime_commit = ""
        engine._hour_refill_evidence = recorder if recorder is not None else AFRecorder()
        engine._roll_over_to_next_hour = lambda: False
        engine._peek_next_hour = lambda: None
        engine._note_active_message_discard = lambda _item: None
        engine._get_deck_position = MagicMock(return_value=100.0)
        for name, value in state.items():
            setattr(engine, name, value)
        return engine

    def quiet(self):
        """emit_event writes the DB; capture_deck_evidence and idle_add are
        the error path's own work. None of them is evidence capture."""
        return [patch.object(eng_module, "emit_event", new=lambda **_k: None),
                patch.object(eng_module, "_active_message_candidate", new=lambda _t: False),
                patch.object(eng_module, "capture_deck_evidence", new=MagicMock(return_value={})),
                patch.object(eng_module.GLib, "idle_add", new=MagicMock(return_value=1))]

    def blocked(self, fn, *args):
        patches = self.quiet()
        for p in patches:
            p.start()
        blocker = _NoQueries()
        try:
            with connection.execute_wrapper(blocker):
                return fn(*args)
        finally:
            for p in patches:
                p.stop()
            self.assertEqual(blocker.calls, [])


class AutomaticSkipHookTests(HookFixtures, TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.build(self.tmp.name)

    def test_a_committed_dequeue_skip_of_missing_media_is_one_cause(self):
        missing, playable = self.row(), self.row(filepath="present")
        engine = self.engine(items=self.loaded())
        item, forced = self.blocked(engine._next_queue_item)
        self.assertEqual((item.id, forced, engine._queue_cursor), (playable.id, False, 2))
        (evidence,) = engine._hour_refill_evidence.offers
        self.assertIsInstance(evidence, AutomaticSkipCauseEvidence)
        fields = evidence.cause_fields()
        self.assertEqual(fields["source_key"], f"automatic_skip:li{missing.id}:t{missing.track_id}")
        self.assertEqual((fields["cause_type"], fields["failure_phase"], fields["failure_reason"]),
                         ("automatic_skip", "pre_air", "file_missing"))
        self.assertEqual(fields["observed_shortening_seconds"], 248.0)
        self.assertEqual((fields["playlist_log_id_snapshot"], fields["log_date"], fields["log_hour"],
                          fields["log_item_id_snapshot"], fields["scheduled_category_id_snapshot"]),
                         (self.log.id, DAY, 14, missing.id, self.gold.id))
        self.assertNotIn("/nonexistent", repr(fields))                         # no media path recorded
        self.assertIsNone(LogItem.objects.get(pk=missing.pk).played_at)       # capture wrote nothing

    def test_no_filepath_and_the_lookahead_skip_and_revisits(self):
        no_path, missing, playable = self.row(filepath="none"), self.row(), self.row(filepath="present")
        engine = self.engine(items=self.loaded())
        self.assertEqual(self.blocked(engine._peek_playable_at_cursor).id, playable.id)   # cursor advanced
        self.assertEqual(engine._queue_cursor, 2)
        self.assertEqual(self.blocked(engine._peek_playable_at_cursor).id, playable.id)   # nothing re-skipped
        offers = engine._hour_refill_evidence.offers
        self.assertEqual([e.cause_fields()["failure_reason"] for e in offers], ["no_filepath", "file_missing"])
        engine._queue_cursor = 0                                                          # a reload revisits
        self.blocked(engine._next_queue_item)
        keys = [e.source_key for e in offers]
        self.assertEqual(len(keys), 4)
        self.assertEqual(len(set(keys)), 2)          # the same logical keys: the unique key collapses them
        self.assertEqual(keys[0], f"automatic_skip:li{no_path.id}:t{no_path.track_id}")

    def test_preview_and_cache_warming_never_record(self):
        self.row(), self.row(filepath="present")
        engine = self.engine(items=self.loaded())
        preview = self.blocked(engine._get_upcoming_preview)
        self.assertEqual(len(preview), 1)
        self.assertEqual((engine._queue_cursor, engine._hour_refill_evidence.offers), (0, []))

    def test_capture_off_costs_nothing(self):
        self.row(), self.row(filepath="present")
        recorder = AFRecorder(skip=False)
        engine = self.engine(recorder, items=self.loaded())
        with patch.object(eng_module, "automatic_skip_evidence_from_engine") as builder:
            self.blocked(engine._next_queue_item)
        builder.assert_not_called()
        self.assertEqual(recorder.offers, [])

    def assert_no_cause(self, engine, fn=None):
        self.blocked(fn or engine._next_queue_item)
        self.assertEqual(engine._hour_refill_evidence.offers, [])
        self.assertEqual(engine._hour_refill_evidence.snapshot_errors, 0)

    def test_non_qualifying_skips(self):
        draft = PlaylistLog.objects.create(date=DAY, hour=15, status="draft")
        cases = {
            "track deleted (NULL FK)": dict(track=False),
            "operator inserted": dict(origin=LogItem.ORIGIN_OPERATOR_INSERTED),
            "live fill": dict(origin=LogItem.ORIGIN_LIVE_FILL),
            "engine inserted (dedication/alert)": dict(origin=LogItem.ORIGIN_ENGINE_INSERTED),
            "refill installed": dict(origin=LogItem.ORIGIN_REFILL_INSTALLED),
            "unknown origin": dict(origin=None),
            "non-music slot": dict(category="legal"),
            "unapproved log": dict(log=draft),
            "already claimed": dict(playback_claimed_at=timezone.now()),
            "already aired": dict(played_at=timezone.now(), playback_claimed_at=timezone.now()),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                LogItem.objects.all().delete()
                if kwargs.get("category") == "legal":
                    kwargs["category"] = self.legal
                log = kwargs.get("log") or self.log
                self.row(**kwargs)
                self.row(filepath="present", log=log)
                engine = self.engine(items=self.loaded(log))
                self.assert_no_cause(engine)

    def test_forced_resume_and_poison_skips_are_not_causes(self):
        forced, resume, poisoned, playable = self.row(), self.row(), self.row(), self.row(filepath="present")
        items = self.loaded()
        engine = self.engine(items=[items[2], items[3]])
        engine._forced_next_items = [items[0]]
        engine._resume_item = items[1]
        engine._resume_hint = {"log_item_id": items[1].id, "track_id": items[1].track_id}
        engine._poison_skip_identities = [(poisoned.playlist_log_id, poisoned.id)]
        with patch.object(engine, "_report_poison_hit_once"):
            item, _forced = self.blocked(engine._next_queue_item)
        self.assertEqual(item.id, playable.id)
        self.assertEqual(engine._hour_refill_evidence.offers, [])

    def test_resumed_rows_without_cached_relations_fail_closed_without_a_query(self):
        self.row(), self.row(filepath="present")
        bare = list(LogItem.objects.select_related("track").filter(playlist_log=self.log).order_by("position"))
        engine = self.engine(items=bare)
        self.assert_no_cause(engine)

    def test_a_snapshot_failure_never_affects_the_skip(self):
        self.row(), self.row(filepath="present")
        engine = self.engine(items=self.loaded())
        with patch.object(eng_module, "automatic_skip_evidence_from_engine", side_effect=RuntimeError("boom")):
            item, _ = self.blocked(engine._next_queue_item)
        self.assertEqual(engine._queue_cursor, 2)
        self.assertEqual(engine._hour_refill_evidence.snapshot_errors, 1)
        self.assertIsNotNone(item)


class MediaFailureHookTests(HookFixtures, TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.build(self.tmp.name)
        self.song = self.row(filepath="present", playback_claimed_at=timezone.now())
        self.items = self.loaded()

    def deck(self, engine, slot="A", **extra):
        deck = FailureDeck(slot, self.items[0], **extra)
        engine.decks[slot] = deck
        return deck

    def error(self, engine, deck, message=None):
        return self.blocked(engine._on_deck_error, None, message or decode_error(), deck)

    def test_a_pre_air_decoder_failure_is_one_pre_air_cause(self):
        engine = self.engine()
        deck = self.deck(engine)
        self.assertTrue(self.error(engine, deck))
        (evidence,) = engine._hour_refill_evidence.offers
        self.assertIsInstance(evidence, MediaFailureCauseEvidence)
        fields = evidence.cause_fields()
        self.assertEqual((fields["failure_phase"], fields["failure_reason"], fields["observed_shortening_seconds"]),
                         ("pre_air", "stream_decode", 248.0))
        self.assertEqual(fields["source_key"], f"media_failure:li{self.song.id}:Ag17:{ENGINE}")
        self.assertIsNone(fields["failure_position_seconds"])
        engine._get_deck_position.assert_not_called()
        self.assertEqual(deck.completion_reason, "error")
        self.assertIn("ERROR_OBSERVED", deck.milestones)                  # the error path itself ran

    def test_a_post_air_decoder_failure_measures_the_real_position(self):
        engine = self.engine()
        deck = self.deck(engine).aired()
        self.error(engine, deck)
        (evidence,) = engine._hour_refill_evidence.offers
        fields = evidence.cause_fields()
        self.assertEqual((fields["failure_phase"], fields["failure_position_seconds"],
                          fields["observed_shortening_seconds"]), ("post_air", 100.0, 150.0))
        engine._get_deck_position.assert_called_once_with(deck)

    def test_post_air_at_or_after_the_end_or_unknown_position_is_not_a_cause(self):
        for label, position in [("at the end", 250.0), ("past the end", 300.0)]:
            with self.subTest(label):
                engine = self.engine(_get_deck_position=MagicMock(return_value=position))
                self.error(engine, self.deck(engine).aired())
                self.assertEqual(engine._hour_refill_evidence.offers, [])
        engine = self.engine(_get_deck_position=MagicMock(side_effect=RuntimeError("no position")))
        deck = self.deck(engine).aired()
        self.error(engine, deck)
        self.assertEqual((engine._hour_refill_evidence.offers, engine._hour_refill_evidence.snapshot_errors), ([], 1))
        self.assertEqual(deck.completion_reason, "error")                 # the error path itself ran

    def test_non_media_errors_are_not_causes(self):
        Gst.init(None)
        for label, message in [
            ("generic flow failure", ErrorMessage(Gst.StreamError.quark(), Gst.StreamError.FAILED)),
            ("seek", ErrorMessage(Gst.ResourceError.quark(), Gst.ResourceError.SEEK)),
            ("core", ErrorMessage(Gst.CoreError.quark(), Gst.CoreError.FAILED)),
        ]:
            with self.subTest(label):
                engine = self.engine()
                deck = self.deck(engine)
                self.error(engine, deck, message)
                self.assertEqual(engine._hour_refill_evidence.offers, [])
                self.assertEqual(deck.completion_reason, "error")

    def test_ownership_and_generation_exclusions(self):
        cases = {
            "start next active": (dict(_start_next_active={"request_id": "r"}), {}),
            "voice track": (dict(_vt={"phase": "outro_playing", "outgoing_track_id": -1}), {}),
            "manual": (dict(manual_mode=True), {}),
            "mic": (dict(mic_live=True), {}),
            "remote dj": (dict(remote_dj_session=SimpleNamespace(slot_id=None, connection_attempt=None)), {}),
            "gated seek": ({}, dict(gated_seek={"phase": "confirming"})),
            "seek/resume continuation": ({}, dict(continuation_reason="manual_seek_or_resume")),
            "restart resume": ({}, dict(continuation_reason="auto_resume", air_start_eligible=False)),
            "air start dispatched, unrecorded": ({}, dict(air_start_dispatched=True)),
        }
        for label, (state, deck_extra) in cases.items():
            with self.subTest(label):
                engine = self.engine(**state)
                deck = self.deck(engine, **deck_extra)
                self.error(engine, deck)
                self.assertEqual(engine._hour_refill_evidence.offers, [])
        with self.subTest("crossfade"):
            engine = self.engine()
            deck = self.deck(engine)
            engine.decks["B"] = FailureDeck("B", self.items[0], generation=18)
            self.error(engine, deck)
            self.assertEqual(engine._hour_refill_evidence.offers, [])
        with self.subTest("superseded generation"):
            engine = self.engine()
            deck = FailureDeck("A", self.items[0])
            engine.decks["A"] = FailureDeck("A", self.items[0], generation=18)
            self.error(engine, deck)
            self.assertEqual(engine._hour_refill_evidence.offers, [])

    def test_paused_stale_duplicate_and_already_claimed_generations(self):
        for label, extra in [("paused", dict(paused=True)), ("finished", dict(finished=True)),
                             ("retiring", dict(retirement_started=True)),
                             ("eject or EOS already owns it", dict(completion_claimed=True, completion_reason="eject"))]:
            with self.subTest(label):
                engine = self.engine()
                self.error(engine, self.deck(engine, **extra))
                self.assertEqual(engine._hour_refill_evidence.offers, [])
        engine = self.engine()
        deck = self.deck(engine)
        self.error(engine, deck)
        self.error(engine, deck)                                          # a duplicate error callback
        self.assertEqual(len(engine._hour_refill_evidence.offers), 1)

    def test_capture_off_costs_nothing(self):
        engine = self.engine(AFRecorder(failure=False))
        deck = self.deck(engine).aired()
        with patch.object(eng_module, "media_failure_evidence_from_engine") as builder:
            self.error(engine, deck)
        builder.assert_not_called()
        engine._get_deck_position.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Real GStreamer decks
# ---------------------------------------------------------------------------

class MediaFailureTransportTests(TestCase):
    """The real error path on real decks with saved rows loaded as the queue
    loads them; the snapshot itself runs under a query blocker."""

    def setUp(self):
        from library.tests._transport_harness import TransportHarness

        self.h = TransportHarness(self)
        self.engine = self.h.engine
        self.recorder = AFRecorder()
        self.engine._hour_refill_evidence = self.recorder
        self.engine._schedule_occurrence_air_start_from_probe = (
            lambda deck: setattr(deck, "air_start_dispatched", True) or setattr(deck, "air_start_recorded", True))
        self.engine._persist_media_incident = lambda _evidence: None
        music, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.category = Category.objects.create(code="AF_GOLD", name="Gold", kind=music)
        self.artist = Artist.objects.create(name="Harness Artist")
        self.snapshots = []
        real = eng_module.media_failure_evidence_from_engine

        def spy(engine, deck, domain, code, *, occurred_at):
            blocker = _NoQueries()
            with connection.execute_wrapper(blocker):
                result = real(engine, deck, domain, code, occurred_at=occurred_at)
            self.assertEqual(blocker.calls, [])
            self.snapshots.append(result[1] if result[0] is None else result[0].phase)
            return result

        for patcher in (patch.object(eng_module, "media_failure_evidence_from_engine", side_effect=spy),
                        patch.object(eng_module, "close_old_connections", new=lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def queue(self, paths_and_seconds):
        log = PlaylistLog.objects.create(date=DAY, hour=14, status="approved")
        for position, (path, seconds) in enumerate(paths_and_seconds):
            track, _ = Track.objects.get_or_create(filepath=str(path), defaults=dict(
                filename=os.path.basename(str(path)), format="wav", title=f"T{position}", artist=self.artist,
                category=self.category, duration_seconds=seconds, next_start_seconds=seconds, cue_in_seconds=0.0))
            LogItem.objects.create(playlist_log=log, position=position, scheduled_time=timezone.now(), track=track,
                                   track_title=track.title, category=self.category,
                                   origin=LogItem.ORIGIN_GENERATED_SCHEDULE)
        rows = list(log.items.select_related(*QUEUE_RELATED).order_by("position"))
        self.engine.log_items, self.engine._queue_cursor = rows, 0
        return [r.id for r in rows]

    def tone(self, name, seconds):
        return self.h.media(name, seconds=seconds)

    def test_a_genuinely_undecodable_file_is_one_pre_air_cause(self):
        from library.tests._transport_harness import pump, wait_until

        garbage = self.h.root / "garbage.wav"
        garbage.write_bytes(os.urandom(64 * 1024))
        ids = self.queue([(garbage, 8.0), (self.tone("next", 8), 8.0)])
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.snapshots, 5.0), self.engine.decks)
        pump(0.5)
        self.assertEqual(self.snapshots, [PHASE_PRE_AIR])
        (evidence,) = self.recorder.offers
        fields = evidence.cause_fields()
        self.assertEqual((fields["log_item_id_snapshot"], fields["failure_phase"]), (ids[0], "pre_air"))
        self.assertTrue(fields["failure_reason"].startswith(("stream_", "resource_")))
        self.assertEqual(fields["observed_shortening_seconds"], 8.0)
        self.assertEqual(self.h.claims, ids)                     # the error path advanced as before

    def post_error(self, deck, domain_quark, code):
        source = next(e for e in deck.pipeline.iterate_recurse() if e.get_factory() is not None)
        source.post_message(Gst.Message.new_error(source, GLib.Error.new_literal(domain_quark, "test", int(code)),
                                                  "posted by test"))

    def start_aired(self, ids):
        from library.tests._transport_harness import pump, wait_until

        self.h.play()
        self.engine._start_next_track(slot="A")
        deck = self.engine.decks["A"]
        self.assertTrue(wait_until(lambda: deck.air_start_recorded, 3.0))
        pump(0.6)
        return deck

    def test_a_real_decoder_error_after_air_is_one_post_air_cause(self):
        from library.tests._transport_harness import pump, wait_until

        ids = self.queue([(self.tone("a", 8), 8.0), (self.tone("b", 8), 8.0)])
        deck = self.start_aired(ids)
        Gst.init(None)
        self.post_error(deck, Gst.StreamError.quark(), Gst.StreamError.DECODE)
        self.assertTrue(wait_until(lambda: self.snapshots, 3.0))
        pump(0.4)
        self.assertEqual(self.snapshots, [PHASE_POST_AIR])
        (evidence,) = self.recorder.offers
        fields = evidence.cause_fields()
        self.assertEqual((fields["log_item_id_snapshot"], fields["deck_generation"]), (ids[0], deck.generation))
        self.assertGreater(fields["failure_position_seconds"], 0.3)
        self.assertLess(fields["failure_position_seconds"], 4.0)
        self.assertAlmostEqual(fields["observed_shortening_seconds"],
                               round(8.0 - fields["failure_position_seconds"], 3), places=3)
        self.assertEqual(self.h.claims, ids)

    def test_a_generic_flow_error_and_an_ordinary_eos_are_not_causes(self):
        from library.tests._transport_harness import pump, wait_until

        ids = self.queue([(self.tone("c", 8), 8.0), (self.tone("short", 1), 1.0), (self.tone("d", 8), 8.0)])
        deck = self.start_aired(ids)
        Gst.init(None)
        self.post_error(deck, Gst.StreamError.quark(), Gst.StreamError.FAILED)
        self.assertTrue(wait_until(lambda: self.snapshots, 3.0))
        self.assertEqual(self.snapshots, ["not_media_error"])
        self.assertTrue(wait_until(lambda: len(self.h.claims) >= 2, 3.0))
        pump(2.0)                                                   # the 1 s item ends by ordinary EOS
        self.assertEqual(self.snapshots, ["not_media_error"])
        self.assertEqual(self.recorder.offers, [])


    def test_an_early_eos_against_stale_metadata_is_not_a_cause(self):
        from library.tests._transport_harness import pump, wait_until

        # The Track claims 8 s; the file holds 1 s. An ordinary early EOS.
        ids = self.queue([(self.tone("early", 1), 8.0), (self.tone("e", 8), 8.0)])
        self.start_aired(ids)
        self.assertTrue(wait_until(lambda: len(self.h.claims) >= 2, 4.0), self.h.claims)
        pump(0.5)
        self.assertEqual(self.snapshots, [])              # the error hook never ran
        self.assertEqual(self.recorder.offers, [])


# ---------------------------------------------------------------------------
# 4. Worker
# ---------------------------------------------------------------------------

def _skip_evidence(n=1):
    return AutomaticSkipCauseEvidence(occurrence(log_item_id=9000 + n, track_id=600 + n), "file_missing", 248.0,
                                      timezone.now())


def _failure_evidence(n=1, phase=PHASE_POST_AIR):
    return MediaFailureCauseEvidence(occurrence(log_item_id=8000 + n, track_id=700 + n, claimed=True), ENGINE, "A",
                                     n, phase, "stream_decode", 150.0,
                                     100.0 if phase == PHASE_POST_AIR else None, timezone.now())


class AutomaticWorkerTests(TransactionTestCase):
    def recorder(self, flags, **kwargs):
        recorder = HourRefillEvidenceRecorder(read_flags=lambda: flags, policy_refresh_seconds=60, **kwargs)
        recorder.start()
        self.addCleanup(recorder.stop)
        self.assertTrue(recorder.wait_until_ready(5))
        return recorder

    def drain(self, recorder, key, expected):
        self.assertTrue(_wait(lambda: recorder.counters[key] >= expected), recorder.counters)

    def test_each_switch_is_independent(self):
        for flags, skip, fail in [((True, True, True, False, False), 0, 0), ((False, False, False, True, False), 1, 0),
                                  ((False, False, False, False, True), 0, 2), ((True,) * 5, 1, 2)]:
            with self.subTest(flags):
                HourRefillCause.objects.all().delete()
                recorder = self.recorder(flags)
                recorder.offer(_skip_evidence())
                recorder.offer(_failure_evidence(1))
                recorder.offer(_failure_evidence(2, PHASE_PRE_AIR))
                self.drain(recorder, "recorded", skip + fail)
                self.drain(recorder, "dropped_policy_off", 3 - skip - fail)
                self.assertEqual(HourRefillCause.objects.filter(cause_type="automatic_skip").count(), skip)
                self.assertEqual(HourRefillCause.objects.filter(cause_type="media_failure").count(), fail)
                recorder.stop()

    def test_shorter_readers_leave_the_new_switches_off(self):
        for flags in [(True, True), (True, True, True)]:
            with self.subTest(len(flags)):
                recorder = self.recorder(flags)
                self.assertEqual((recorder.capture_automatic_skip, recorder.capture_media_failure), (False, False))
                recorder.stop()

    def test_duplicates_collapse_and_errors_are_counted(self):
        recorder = self.recorder((False, False, False, True, True))
        recorder.offer(_skip_evidence())
        recorder.offer(_skip_evidence())                           # a reload's repeat skip
        recorder.offer(_failure_evidence(1))
        recorder.offer(_failure_evidence(1))                       # a duplicate error callback
        self.drain(recorder, "duplicate", 2)
        self.assertEqual(HourRefillCause.objects.count(), 2)
        recorder.stop()

        def broken(_e):
            raise RuntimeError("db down")

        recorder = self.recorder((False, False, False, True, True), insert=broken)
        self.assertTrue(recorder.offer(_skip_evidence(5)))
        self.drain(recorder, "dropped_error", 1)

    def test_the_default_reader_reads_the_row_and_fails_closed(self):
        recorder = HourRefillEvidenceRecorder()
        recorder._refresh_policy()
        self.assertEqual((recorder.capture_automatic_skip, recorder.capture_media_failure), (False, False))
        HourRefillPolicy(enabled=True, capture_automatic_skip=True, capture_media_failure=True).save()
        recorder._refresh_policy()
        self.assertEqual((recorder.capture_automatic_skip, recorder.capture_media_failure), (True, True))
        HourRefillPolicy.objects.filter(pk=1).update(enabled=False)
        recorder._refresh_policy()
        self.assertEqual((recorder.capture_automatic_skip, recorder.capture_media_failure), (False, False))
        HourRefillPolicy.objects.filter(pk=1).update(enabled=True)
        with patch.object(HourRefillPolicy, "capture_switches", side_effect=RuntimeError("unreadable")):
            recorder._refresh_policy()
        self.assertEqual((recorder.capture_requests, recorder.capture_automatic_skip,
                          recorder.capture_media_failure), (False, False, False))

    def test_a_stopped_or_full_worker_drops_without_blocking(self):
        recorder = HourRefillEvidenceRecorder(read_flags=lambda: (True,) * 5, max_queued=1)
        self.assertFalse(recorder.offer(_skip_evidence()))           # not running
        self.assertEqual(recorder.counters["dropped_not_running"], 1)


# ---------------------------------------------------------------------------
# 5. Schema and migration
# ---------------------------------------------------------------------------

class AutomaticSchemaTests(TestCase):
    def test_complete_rows_are_accepted_and_append_only(self):
        for evidence in (_skip_evidence(), _failure_evidence(1), _failure_evidence(2, PHASE_PRE_AIR)):
            row = HourRefillCause.objects.create(**evidence.cause_fields())
            with self.assertRaises(ValueError):
                row.save()

    def test_checks_reject_incomplete_identity(self):
        bad = {
            "skip without reason": (_skip_evidence(), dict(failure_reason="")),
            "skip wrong phase": (_skip_evidence(), dict(failure_phase="post_air")),
            "skip without track": (_skip_evidence(), dict(original_track_id_snapshot=None)),
            "failure without engine": (_failure_evidence(), dict(engine_instance="")),
            "failure without slot": (_failure_evidence(), dict(deck_slot="")),
            "failure without generation": (_failure_evidence(), dict(deck_generation=None)),
            "failure unknown phase": (_failure_evidence(), dict(failure_phase="mid_air")),
            "failure without reason": (_failure_evidence(), dict(failure_reason="")),
            "post-air without position": (_failure_evidence(), dict(failure_position_seconds=None)),
            "unknown type": (_skip_evidence(), dict(cause_type="watchdog_stall")),
        }
        for label, (evidence, overrides) in bad.items():
            fields = dict(evidence.cause_fields(), source_key=f"k-{label}", **overrides)
            with self.subTest(label), self.assertRaises(IntegrityError), transaction.atomic():
                HourRefillCause.objects.create(**fields)

    def test_policy_defaults_off_and_earlier_flag_shapes_are_unchanged(self):
        self.assertEqual(HourRefillPolicy.capture_flags(), (False, False))
        self.assertEqual(HourRefillPolicy.capture_switches(), (False, False, False))
        self.assertEqual(HourRefillPolicy.capture_switches(include_automatic=True), (False,) * 5)
        policy = HourRefillPolicy()
        self.assertEqual((policy.capture_automatic_skip, policy.capture_media_failure), (False, False))

    def test_0098_operations_and_update_center_classification(self):
        plan = _serialize_exact_plan(MigrationLoader(connection), ["library.0098_hour_refill_automatic_failure_cause"])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[0]["operations"]], [
            ("AddField", "additive"), ("AddField", "additive"), ("AddField", "additive"), ("AddField", "additive"),
            ("AddField", "additive"), ("AlterField", "additive"),
            ("RemoveConstraint", "manual"), ("AddConstraint", "manual"), ("AddConstraint", "manual"),
            ("AddConstraint", "manual"),
        ])

    def test_0098_writes_no_data_and_enables_nothing(self):
        from pathlib import Path

        source = (Path(settings.BASE_DIR) / "library/migrations/0098_hour_refill_automatic_failure_cause.py").read_text()
        body = source.split("operations = [")[1]
        for forbidden in ("RunPython", "RunSQL", "update(", "objects", "default=True", "delete"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, body)


class AutomaticMigrationTests(TransactionTestCase):
    def tearDown(self):
        call_command("migrate", verbosity=0)

    def test_old_schema_fails_closed_and_reverse_never_deletes_evidence(self):
        call_command("migrate", "library", "0097", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO library_hourrefillpolicy (id, enabled, capture_request_substitution, "
                "capture_operator_eject, capture_start_next, dry_run_only, updated_at) "
                "VALUES (1, true, true, true, true, false, now())")
        with ARMED:
            self.assertFalse(inst.install_permitted(cause_types=(CAUSE_OPERATOR_EJECT,)))
        self.assertEqual(runner_mod.read_policy_state().reason, "policy_unreadable")
        recorder = HourRefillEvidenceRecorder()
        recorder._refresh_policy()
        self.assertEqual((recorder.capture_eject, recorder.capture_media_failure), (False, False))
        self.assertEqual(HourRefillPolicy.capture_switches(), (True, True, True))   # r0121 Start Next reader intact
        call_command("migrate", "library", "0098", verbosity=0)
        self.assertEqual(HourRefillPolicy.capture_switches(include_automatic=True), (True, True, True, False, False))
        HourRefillCause.objects.create(**_skip_evidence().cause_fields())
        with self.assertRaises(IntegrityError):
            call_command("migrate", "library", "0097", verbosity=0)
        self.assertEqual(HourRefillCause.objects.filter(cause_type="automatic_skip").count(), 1)   # kept


# ---------------------------------------------------------------------------
# 6. Trace, projection, thresholds, policy
# ---------------------------------------------------------------------------

def auto(cause_id, log_item_id, *, kind=CAUSE_AUTOMATIC_SKIP, phase=PHASE_PRE_AIR, shortening=240.0, minute=30,
         track=None, log=LOG, hour=14):
    return CauseRecord(
        id=cause_id, source_key=f"{kind}:li{log_item_id}:{cause_id}", cause_type=kind, occurred_at=at(14, minute),
        log_id=log, log_date=DAY, log_hour=hour, log_item_id=log_item_id, log_item_position=log_item_id,
        scheduled_category_id=GOLD, scheduled_category_code=f"C{GOLD}", scheduled_category_kind_code="music",
        original_track_id=1000 + log_item_id if track is None else track, original_effective_airtime_seconds=240.0,
        replacement_track_id=None, replacement_effective_airtime_seconds=None,
        observed_shortening_seconds=shortening, failure_phase=phase, failure_reason="file_missing",
    )


def window(future_seconds, *, skipped=None, played=True):
    """14:40 -> 15:00 = 1200 s: item 1 played, item 2 skipped/failed
    (unclaimed and unplayed unless told), 3 on the deck (240 s)."""
    first = item(1, played=True, claimed=True)
    second = item(2, played=played, claimed=played) if skipped is None else skipped
    on_deck = item(3, 240.0, played=True, claimed=True)
    future = [item(4 + i, s) for i, s in enumerate(future_seconds)]
    return snapshot([first, second, on_deck] + future, 3, [deck_state(on_deck, 10.0)])


class AutomaticProjectionTests(SimpleTestCase):
    def project(self, snap, causes):
        return proj.project_refill(snap, causes=causes, boundary=boundary(), allowance=0)

    def test_partition_rules(self):
        unplayed = window([290.0], played=False)
        aired = window([290.0], played=True)
        cases = {
            "skip, never aired": (unplayed, auto(1, 2), None),
            "skip that later aired": (aired, auto(1, 2), "occurrence_aired"),
            "skip claimed since": (window([290.0], skipped=item(2, claimed=True)), auto(1, 2), "occurrence_aired"),
            "skip still queued": (unplayed, auto(1, 4), "occurrence_still_pending"),
            "skip track changed": (unplayed, auto(1, 2, track=9999), "superseded"),
            "pre-air failure, never aired": (window([290.0], skipped=item(2, claimed=True)),
                                             auto(1, 2, kind=CAUSE_MEDIA_FAILURE), None),
            "pre-air failure that aired": (aired, auto(1, 2, kind=CAUSE_MEDIA_FAILURE), "occurrence_aired"),
            "post-air failure": (aired, auto(1, 2, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR), None),
            "failure on the deck": (aired, auto(1, 3, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR),
                                    "occurrence_still_pending"),
            "other hour": (unplayed, auto(1, 2, hour=13), "other_hour"),
            "other log": (unplayed, auto(1, 2, log=LOG + 1), "stale_log"),
        }
        for label, (snap, cause, reason) in cases.items():
            with self.subTest(label):
                qualified, excluded = proj.partition_causes(snap, [cause])
                if reason is None:
                    self.assertEqual([c.cause_type for c in qualified], [cause.cause_type])
                else:
                    self.assertEqual((qualified, excluded), ((), ((cause.source_key, reason),)))

    def test_thresholds_are_strict_proposed_60_and_the_strictest_wins(self):
        snap_for = lambda net: window([1200.0 - 230.0 - net], played=False)          # noqa: E731
        cases = [
            ([auto(1, 2)], 60.0, "below_threshold", "automatic_failure_proposed"),
            ([auto(1, 2)], 60.5, "eligible", "automatic_failure_proposed"),
            ([auto(1, 2, kind=CAUSE_MEDIA_FAILURE)], 45.0, "below_threshold", "automatic_failure_proposed"),
            ([auto(1, 2, kind=CAUSE_MEDIA_FAILURE), eject(2, 1)], 45.0, "below_threshold",
             "automatic_failure_proposed"),                                      # not the Eject 30 s
            ([auto(1, 2), swap(3, 4, 1004)], 60.0, "below_threshold", "request_substitution_present"),
            ([eject(2, 1)], 31.0, "eligible", "operator_eject_only_provisional"),        # r0120 unchanged
            ([start_next_cut(5, 1)], 31.0, "eligible", "start_next_only_provisional"),   # r0121 unchanged
        ]
        for causes, net, outcome, basis in cases:
            with self.subTest(types=[c.cause_type for c in causes], net=net):
                p = self.project(snap_for(net), causes)
                self.assertEqual((p.outcome, p.threshold_basis, p.net_shortage_seconds), (outcome, basis, net))

    def test_one_termination_cause_per_occurrence(self):
        trace = build_causality_trace(
            [auto(1, 2, minute=10), auto(2, 2, kind=CAUSE_MEDIA_FAILURE, minute=11), eject(3, 2),
             start_next_cut(4, 2, minute=31)],
            active_log_date=DAY, active_log_hour=14)
        self.assertEqual([r.cause_type for r in trace.causes], ["automatic_skip"])
        self.assertEqual({reason for _k, reason in trace.duplicates}, {"duplicate_occurrence_cause"})
        self.assertEqual(len(trace.duplicates), 3)
        # A request substitution and a later failure are different acts.
        trace = build_causality_trace([swap(1, 2, 1002, shortening=60.0),
                                       auto(2, 2, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR,
                                            shortening=90.0, minute=32)],
                                      active_log_date=DAY, active_log_hour=14)
        self.assertEqual(trace.observed_shortening_seconds, 150.0)

    def test_policy_state_needs_every_participating_switch(self):
        state = runner_mod.PolicyState("observation", "x", request_cause=True, eject_cause=True,
                                       start_next_cause=True, automatic_skip_cause=True)
        self.assertTrue(state.allows([CAUSE_AUTOMATIC_SKIP, CAUSE_OPERATOR_EJECT]))
        self.assertFalse(state.allows([CAUSE_AUTOMATIC_SKIP, CAUSE_MEDIA_FAILURE]))
        self.assertFalse(state.allows([CAUSE_MEDIA_FAILURE]))
        self.assertFalse(state.allows(["watchdog_stall"]))
        self.assertEqual((state.as_dict()["automatic_skip_cause"], state.as_dict()["media_failure_cause"]),
                         (True, False))


class QualifiedSelectionTests(SimpleTestCase):
    """r0121 P1 (Codex): qualify each cause against the CURRENT occurrence
    before selecting one per occurrence family. Item 2 is the lost song."""

    def project(self, snap, causes):
        return proj.project_refill(snap, causes=causes, boundary=boundary(), allowance=0)

    def selected(self, snap, causes):
        qualified, excluded = proj.partition_causes(snap, causes)
        return [c.source_key for c in qualified], dict(excluded)

    def test_skip_then_reload_air_then_post_air_failure_counts_the_failure(self):
        aired = window([290.0], played=True)                 # reloaded, aired, then failed
        skip = auto(1, 2, minute=10)
        fail = auto(2, 2, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR, shortening=90.0, minute=40)
        keys, reasons = self.selected(aired, [skip, fail])
        self.assertEqual(keys, [fail.source_key])
        self.assertEqual(reasons, {skip.source_key: "occurrence_aired"})
        p = self.project(aired, [skip, fail])
        self.assertEqual((p.outcome, p.observed_shortening_seconds, p.threshold_basis),
                         ("eligible", 90.0, "automatic_failure_proposed"))   # the failure's REMAINING time

    def test_a_still_valid_earlier_skip_wins_and_nothing_is_counted_twice(self):
        unclaimed = window([290.0], played=False)
        skip = auto(1, 2, minute=10)
        later = [auto(2, 2, kind=CAUSE_MEDIA_FAILURE, minute=20), eject(3, 2), start_next_cut(4, 2, minute=31)]
        keys, reasons = self.selected(unclaimed, [skip] + later)
        self.assertEqual(keys, [skip.source_key])
        self.assertEqual({reasons[c.source_key] for c in later}, {"duplicate_occurrence_cause"})
        self.assertEqual(self.project(unclaimed, [skip] + later).observed_shortening_seconds, 240.0)

    def test_several_valid_terminations_for_one_occurrence_give_one_in_causal_order(self):
        aired = window([290.0], played=True)
        causes = [eject(1, 2, shortening=100.0, minute=20), start_next_cut(2, 2, shortening=90.0, minute=21),
                  auto(3, 2, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR, shortening=80.0, minute=22)]
        for order in (causes, list(reversed(causes)), [causes[1], causes[2], causes[0]]):
            with self.subTest([c.cause_type for c in order]):
                keys, reasons = self.selected(aired, order)
                self.assertEqual(keys, [causes[0].source_key])
                self.assertEqual(sorted(reasons), sorted(c.source_key for c in causes[1:]))
                self.assertEqual(self.project(aired, order).observed_shortening_seconds, 100.0)

    def test_request_substitution_and_a_later_failure_are_distinct_and_use_the_strict_rule(self):
        snap_for = lambda net: window([1200.0 - 230.0 - net], played=True)       # noqa: E731
        request = swap(1, 2, 1002, shortening=60.0)
        fail = auto(2, 2, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR, shortening=90.0, minute=40)
        keys, _ = self.selected(snap_for(61.0), [request, fail])
        self.assertEqual(keys, [request.source_key, fail.source_key])
        for net, outcome in [(60.0, "below_threshold"), (60.5, "eligible")]:
            with self.subTest(net=net):
                p = self.project(snap_for(net), [fail, request])
                self.assertEqual((p.outcome, p.threshold_basis, p.net_shortage_seconds),
                                 (outcome, "request_substitution_present", net))   # fresh net, not 150 s observed

    def test_all_candidates_invalid_means_no_cause_and_no_fill(self):
        aired = window([290.0], played=True)
        causes = [auto(1, 2, minute=10),                                          # aired since
                  auto(2, 3, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR),     # still on the deck
                  eject(3, 2, log=LOG + 1),                                       # other log
                  auto(4, 99, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR),    # occurrence gone
                  auto(5, 2, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_PRE_AIR)]      # "never aired" but aired
        keys, reasons = self.selected(aired, causes)
        self.assertEqual(keys, [])
        self.assertEqual([reasons[c.source_key] for c in causes],
                         ["occurrence_aired", "occurrence_still_pending", "stale_log", "missing_occurrence",
                          "occurrence_aired"])
        self.assertEqual(self.project(aired, causes).outcome, "no_qualifying_cause")

    def test_replays_and_input_order_never_select_an_invalid_first_cause_or_inflate(self):
        aired = window([290.0], played=True)
        skip = auto(1, 2, minute=10)
        fail = auto(2, 2, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR, shortening=90.0, minute=40)
        replay = replace(fail, id=7)                                     # the same source_key, another row
        for order in ([skip, fail, replay], [replay, fail, skip], [fail, skip, replay, skip]):
            with self.subTest(order=[c.id for c in order]):
                keys, reasons = self.selected(aired, order)
                self.assertEqual(keys, [fail.source_key])
                self.assertEqual(self.project(aired, order).observed_shortening_seconds, 90.0)
                self.assertIn("duplicate_source_key", reasons.values())

    def test_switches_and_unknown_types_stay_fail_closed_on_the_qualified_set(self):
        aired = window([290.0], played=True)
        fail = auto(2, 2, kind=CAUSE_MEDIA_FAILURE, phase=PHASE_POST_AIR, shortening=90.0, minute=40)
        other = eject(3, 1)                                              # a different, valid occurrence
        qualified, _ = proj.partition_causes(aired, [auto(1, 2, minute=10), fail, other])
        types = sorted({c.cause_type for c in qualified})
        self.assertEqual(types, ["media_failure", "operator_eject"])
        for switches, allowed in [(dict(media_failure_cause=True, eject_cause=True), True),
                                  (dict(media_failure_cause=False, eject_cause=True), False),
                                  (dict(media_failure_cause=True, eject_cause=False), False)]:
            with self.subTest(switches):
                self.assertEqual(runner_mod.PolicyState("observation", "x", **switches).allows(types), allowed)
        unknown = replace(fail, cause_type="watchdog_stall", source_key="watchdog_stall:li2:1")
        qualified, excluded = proj.partition_causes(aired, [unknown])
        self.assertEqual((qualified, dict(excluded)), ((), {unknown.source_key: "unknown_cause_type"}))


class AutomaticPolicyGateTests(TestCase):
    def test_runner_and_installer_gates(self):
        HourRefillPolicy(enabled=True, capture_automatic_skip=True, dry_run_only=False).save()
        state = runner_mod.read_policy_state()
        self.assertEqual((state.automatic_skip_cause, state.media_failure_cause), (True, False))
        with ARMED:
            self.assertTrue(inst.install_permitted(cause_types=(CAUSE_AUTOMATIC_SKIP,)))
            self.assertFalse(inst.install_permitted(cause_types=(CAUSE_AUTOMATIC_SKIP, CAUSE_MEDIA_FAILURE)))
            self.assertFalse(inst.install_permitted(cause_types=(CAUSE_MEDIA_FAILURE,)))
            with transaction.atomic():
                self.assertTrue(inst.install_permitted(cause_types=(CAUSE_AUTOMATIC_SKIP,), lock=True))
        self.assertFalse(inst.install_permitted(cause_types=(CAUSE_AUTOMATIC_SKIP,)))      # not armed


# ---------------------------------------------------------------------------
# 7. Guarded installer
# ---------------------------------------------------------------------------

class AutomaticInstallTests(InstallFixtures, TestCase):
    def setUp(self):
        self.build()

    def cause(self, row, kind):
        occ = ScheduledOccurrence(
            origin_source="scheduled", kind_code="music", log_approved=True, played=kind == CAUSE_MEDIA_FAILURE,
            claimed=kind == CAUSE_MEDIA_FAILURE, log_item_id=row.id, playlist_log_id=row.playlist_log_id,
            log_date=row.playlist_log.date, log_hour=row.playlist_log.hour, position=row.position,
            category_id=row.category_id, category_code=row.category.code, track_id=row.track_id,
            cue_in_seconds=0.0, next_start_seconds=240.0, duration_seconds=240.0, complete=True,
        )
        if kind == CAUSE_AUTOMATIC_SKIP:
            evidence = AutomaticSkipCauseEvidence(occ, "file_missing", 240.0, refill_at(14, 30))
        else:
            evidence = MediaFailureCauseEvidence(occ, "eng-b", "A", row.id, PHASE_POST_AIR, "stream_decode", 120.0,
                                                 120.0, refill_at(14, 30))
        return HourRefillCause.objects.create(**evidence.cause_fields())

    def hour(self, kind, *, lost_played):
        """Phase B's hour (net 389.25 s) with item 1 skipped or failed."""
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        lost = self.row(self.track(240.0), 1, played=lost_played)
        self.row(self.track(240.0), 2, played=True)
        for i, seconds in enumerate((290.0, 290.0)):
            self.row(self.track(seconds), 3 + i)
        self.cause(lost, kind)
        return self.engine(cursor=3, deck_index=2, deck_position=10.0)

    def test_each_new_type_installs_once_under_the_real_policy_gate(self):
        for kind, played, switch in [(CAUSE_AUTOMATIC_SKIP, False, "capture_automatic_skip"),
                                     (CAUSE_MEDIA_FAILURE, True, "capture_media_failure")]:
            with self.subTest(kind):
                HourRefillInstall.objects.all().delete()
                HourRefillCause.objects.all().delete()
                HourRefillPolicy.objects.all().delete()
                LogItem.objects.all().delete()
                PlaylistLog.objects.exclude(pk=self.log.pk).delete()
                engine = self.hour(kind, lost_played=played)
                self.track(200.0), self.track(180.0)
                HourRefillPolicy(enabled=True, dry_run_only=False, capture_request_substitution=True,
                                 capture_operator_eject=True, capture_start_next=True).save()
                before = self.rows()
                with ARMED:
                    refused = self.installer(engine).attempt()
                self.assertEqual((refused.status, refused.reasons),
                                 (inst.STATUS_POLICY_REFUSED, ("not_permitted_for_cause_types",)))
                self.assertEqual(self.rows(), before)
                HourRefillPolicy.objects.filter(pk=1).update(**{switch: True})
                with ARMED:
                    installed = self.installer(engine).attempt()
                self.assertEqual(installed.status, inst.STATUS_INSTALLED, installed.reasons)
                self.assertEqual(HourRefillInstall.objects.count(), 1)

    def test_a_voided_skip_never_hides_a_later_post_air_failure(self):
        """P1 end to end: the skip row (file was missing) and, after the file
        returned and the song aired, a post-air failure of the SAME row."""
        engine = self.hour(CAUSE_MEDIA_FAILURE, lost_played=True)
        lost = LogItem.objects.get(playlist_log=self.log, position=1)
        skip = self.cause(lost, CAUSE_AUTOMATIC_SKIP)
        HourRefillCause.objects.filter(pk=skip.pk).update(occurred_at=refill_at(14, 5))   # earlier in causal order
        failure_row = HourRefillCause.objects.get(cause_type=CAUSE_MEDIA_FAILURE)
        self.track(200.0), self.track(180.0)
        HourRefillPolicy(enabled=True, dry_run_only=False, capture_automatic_skip=True,
                         capture_media_failure=True).save()
        with ARMED:
            result = self.installer(engine).attempt()
        self.assertEqual(result.status, inst.STATUS_INSTALLED, result.reasons)
        self.assertEqual(HourRefillInstall.objects.get().cause_ids, [failure_row.id])

    def test_dry_run_unarmed_and_unsafe_transport_never_write(self):
        engine = self.hour(CAUSE_AUTOMATIC_SKIP, lost_played=False)
        self.track(200.0), self.track(180.0)
        HourRefillPolicy(enabled=True, capture_automatic_skip=True).save()
        before = self.rows()
        with ARMED:
            self.assertEqual(self.installer(engine).attempt().status, inst.STATUS_DRY_RUN_ONLY)
        HourRefillPolicy.objects.filter(pk=1).update(dry_run_only=False)
        self.assertEqual(self.installer(engine).attempt().status, inst.STATUS_DRY_RUN_ONLY)       # not armed
        for change in (lambda e: setattr(e, "_live_fill_in_progress", True),
                       lambda e: setattr(e, "manual_mode", True),
                       lambda e: e.decks.__setitem__("A", None)):
            change(engine)
            with ARMED:
                self.assertNotEqual(self.installer(engine).attempt().status, inst.STATUS_INSTALLED)
        self.assertEqual(self.rows(), before)
        self.assertFalse(HourRefillInstall.objects.exists())


# ---------------------------------------------------------------------------
# 8. Off parity: no policy row, real recorder
# ---------------------------------------------------------------------------

class AutomaticOffParityTests(HookFixtures, TransactionTestCase):
    def test_no_policy_row_means_no_capture_no_cause_no_install(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.build(tmp.name)
        recorder = HourRefillEvidenceRecorder()
        recorder.start()
        self.addCleanup(recorder.stop)
        self.assertTrue(recorder.wait_until_ready(5))
        self.assertEqual((recorder.capture_automatic_skip, recorder.capture_media_failure), (False, False))
        self.row(), self.row(filepath="present", playback_claimed_at=timezone.now())
        items = self.loaded()
        engine = self.engine(recorder, items=items)
        with patch.object(recorder, "offer", wraps=recorder.offer) as offered:
            item, _ = self.blocked(engine._next_queue_item)
            deck = FailureDeck("A", items[1])
            engine.decks["A"] = deck
            self.blocked(engine._on_deck_error, None, decode_error(), deck)
        offered.assert_not_called()
        self.assertEqual(item.id, items[1].id)
        self.assertEqual(deck.completion_reason, "error")
        self.assertFalse(HourRefillCause.objects.exists())
        self.assertFalse(HourRefillInstall.objects.exists())
        self.assertFalse(HourRefillPolicy.objects.exists())
