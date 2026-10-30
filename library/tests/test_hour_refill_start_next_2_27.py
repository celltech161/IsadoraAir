"""Roadmap 2.27, r0121 -- a completed Start Next cut as an hour-refill cause.

Contract: docs/roadmap/2_27_START_NEXT_CAUSALITY_EXTENSION.md.

- Classification: pure decision table on plain values.
- Hook: the real PlaybackEngine._start_next_completion_due / _finish on a
  bare stand-in with real, queue-loaded LogItems, every call under a
  database blocker (the engine side must issue zero queries).
- Transport: the real Start Next state machine on real GStreamer decks
  (library/tests/_transport_harness) with unsaved model rows, so the real
  snapshot classifies them; SimpleTestCase refuses any query.
- Worker, schema, projection, policy and installer against the guarded
  test database.
"""
import threading
import time
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

import library.services.engine as eng_module
from library.models import (
    Artist, Category, CategoryKind, HourRefillCause, HourRefillInstall, HourRefillPolicy, LogItem,
    PlaylistLog, Track,
)
from library.services import hour_refill_install as inst
from library.services import hour_refill_projection as proj
from library.services import hour_refill_runner as runner_mod
from library.services.hour_refill_causality import (
    CAUSE_OPERATOR_EJECT,
    CAUSE_REQUEST_SUBSTITUTION,
    CAUSE_START_NEXT,
    CauseRecord,
    EjectCauseEnvelope,
    EjectCauseEvidence,
    StartNextCauseEnvelope,
    StartNextCauseEvidence,
    StartNextObservation,
    build_causality_trace,
    classify_start_next_observation,
    start_next_source_key,
)
from library.services.hour_refill_evidence import HourRefillEvidenceRecorder
from library.tests.test_hour_refill_eject_worker_2_27 import QUEUE_RELATED, FakeDeck, _NoQueries, _wait
from library.tests.test_hour_refill_install_2_27 import PERMIT, InstallFixtures
from library.tests.test_hour_refill_projection_2_27 import (
    GOLD, LOG, at, boundary, deck as deck_state, eject, item, snapshot, swap,
)
from library.tests.test_hour_refill_proposal_2_27 import DAY as REFILL_DAY
from library.tests.test_hour_refill_proposal_2_27 import at as refill_at
from updatecenter.management.commands.updatecenter_probe import _serialize_exact_plan

ENGINE = "engine-instance-1"
REQUEST = "5f0c2e1d9a8b4c3d2e1f0a9b8c7d6e5f"
ARMED = override_settings(HOUR_REFILL_INSTALL_ARMED=True)


def observation(**overrides):
    """The eligible baseline: a completed fade_and_start cut of a generated
    music occurrence at 100 s (cue_in 2, next_start 250, duration 260)."""
    values = dict(
        engine_instance=ENGINE, request_id=REQUEST, mode="fade_and_start", phase="advancing", slot="A",
        deck_log_item_id=41, deck_generation=17, outgoing={"slot": "A", "log_item_id": 41, "generation": 17},
        deck_is_outgoing=True, deck_mapped=True, completion_claimed=True, incoming_started=True,
        foreign_live_deck=False, ownership_at_accept={"manual": False, "remote_dj": False, "mic": False},
        occurrence_source="scheduled", on_air=True, paused=False, scheduled_category_kind_code="music",
        position_seconds=100.0, cue_in_seconds=2.0, next_start_seconds=250.0, duration_seconds=260.0,
    )
    values.update(overrides)
    return StartNextObservation(**values)


# ---------------------------------------------------------------------------
# 1. Pure classification
# ---------------------------------------------------------------------------

class StartNextClassificationTests(SimpleTestCase):
    def test_the_eligible_cut_is_one_envelope_with_exact_identity(self):
        envelope, reason = classify_start_next_observation(observation())
        self.assertIsNone(reason)
        self.assertEqual(envelope, StartNextCauseEnvelope(
            source_key=start_next_source_key(ENGINE, "A", 17, 41, REQUEST), engine_instance=ENGINE,
            request_id=REQUEST, slot="A", deck_generation=17, log_item_id=41, cut_position_seconds=100.0,
            remaining_effective_airtime_seconds=150.0,
        ))

    def test_effective_time_never_uses_file_length_press_time_or_fade(self):
        cases = [
            # (position, cue_in, next_start, duration, expected)
            (100.0, 2.0, 250.0, 260.0, 150.0),        # next_start is the listener end, not 260
            (100.0, 2.0, None, 260.0, 160.0),         # no next_start: duration
            (1.0, 12.0, 250.0, 260.0, 238.0),         # still inside the cue-in: counts from cue_in
            (249.75, 0.0, 250.0, 260.0, 0.25),        # a 0.25 s early cut is still a cut
            (100.0004, 0.0, 250.0, 260.0, 150.0),     # ms rounding as Phase A
        ]
        for position, cue_in, next_start, duration, expected in cases:
            with self.subTest(position=position, cue_in=cue_in, next_start=next_start):
                envelope, reason = classify_start_next_observation(observation(
                    position_seconds=position, cue_in_seconds=cue_in, next_start_seconds=next_start,
                    duration_seconds=duration,
                ))
                self.assertIsNone(reason)
                self.assertEqual(envelope.remaining_effective_airtime_seconds, expected)

    def test_a_fade_that_reached_the_scheduled_end_is_not_a_cause(self):
        for position in (250.0, 255.0, 260.0, 400.0):
            with self.subTest(position):
                self.assertEqual(classify_start_next_observation(observation(position_seconds=position)),
                                 (None, "no_remaining_airtime"))
        self.assertEqual(classify_start_next_observation(observation(next_start_seconds=None,
                                                                     duration_seconds=None)),
                         (None, "no_remaining_airtime"))

    def test_every_exclusion_has_its_reason(self):
        cases = {
            "overlap": (dict(mode="overlap"), "not_fade_and_start"),
            "start only": (dict(mode="start_only"), "not_fade_and_start"),
            "not claimed": (dict(completion_claimed=False), "not_claimed"),
            "other deck object": (dict(deck_is_outgoing=False), "identity_mismatch"),
            "no longer mapped": (dict(deck_mapped=False), "identity_mismatch"),
            "other generation": (dict(deck_generation=18), "identity_mismatch"),
            "other occurrence": (dict(deck_log_item_id=42), "identity_mismatch"),
            "other slot": (dict(slot="B"), "identity_mismatch"),
            "no acceptance record": (dict(outgoing=None), "identity_mismatch"),
            "no engine instance": (dict(engine_instance=""), "identity_unknown"),
            "no request id": (dict(request_id=""), "identity_unknown"),
            "to silence": (dict(phase="to_silence"), "to_silence"),
            "incoming never started": (dict(incoming_started=False), "incoming_not_started"),
            "still fading": (dict(phase="fading"), "incoming_not_started"),
            "foreign live deck": (dict(foreign_live_deck=True), "foreign_live_deck"),
            "ownership unknown": (dict(ownership_at_accept=None), "ownership_unknown"),
            "ownership malformed": (dict(ownership_at_accept={"manual": False}), "ownership_unknown"),
            "manual at accept": (dict(ownership_at_accept={"manual": True, "remote_dj": False, "mic": False}),
                                 "manual_hold"),
            "manual at the cut": (dict(manual_mode=True), "manual_hold"),
            "remote dj at accept": (dict(ownership_at_accept={"manual": False, "remote_dj": True, "mic": False}),
                                    "remote_dj_owner"),
            "remote dj at the cut": (dict(remote_dj_owner=True), "remote_dj_owner"),
            "mic at accept": (dict(ownership_at_accept={"manual": False, "remote_dj": False, "mic": True}),
                              "mic_live"),
            "mic at the cut": (dict(mic_live=True), "mic_live"),
            "voice track": (dict(vt_phase="outro_playing"), "vt_transition"),
            "gated seek": (dict(seek_in_progress=True), "seek_in_progress"),
            "continuation": (dict(occurrence_source="continuation"), "not_scheduled_occurrence"),
            "inserted": (dict(occurrence_source="operator_inserted"), "not_scheduled_occurrence"),
            "unknown origin": (dict(occurrence_source="unknown"), "not_scheduled_occurrence"),
            "non-music": (dict(scheduled_category_kind_code="legal_id"), "not_music"),
            "kind not loaded": (dict(scheduled_category_kind_code=""), "not_music"),
            "not on air": (dict(on_air=False), "not_on_air"),
            "paused": (dict(paused=True), "paused"),
            "no position": (dict(position_seconds=None), "position_unknown"),
            "negative position": (dict(position_seconds=-1.0), "position_unknown"),
            "nan position": (dict(position_seconds=float("nan")), "position_unknown"),
            "bool position": (dict(position_seconds=True), "position_unknown"),
        }
        for label, (overrides, reason) in cases.items():
            with self.subTest(label):
                self.assertEqual(classify_start_next_observation(observation(**overrides)), (None, reason))

    def test_source_key_is_bounded_deterministic_and_private(self):
        long_instance, long_request = "e" * 64, "r" * 64
        key = start_next_source_key(long_instance, "B", 9_223_372_036_854_775_807,
                                    9_223_372_036_854_775_807, long_request)
        self.assertLessEqual(len(key), 160)
        self.assertLess(len(start_next_source_key(ENGINE, "A", 17, 41, REQUEST)), 100)
        self.assertNotIn(long_request, key)
        self.assertEqual(key, start_next_source_key(long_instance, "B", 9_223_372_036_854_775_807,
                                                    9_223_372_036_854_775_807, long_request))
        base = start_next_source_key(ENGINE, "A", 17, 41, REQUEST)
        for changed in (start_next_source_key("engine-instance-2", "A", 17, 41, REQUEST),
                        start_next_source_key(ENGINE, "B", 17, 41, REQUEST),
                        start_next_source_key(ENGINE, "A", 18, 41, REQUEST),
                        start_next_source_key(ENGINE, "A", 17, 42, REQUEST),
                        start_next_source_key(ENGINE, "A", 17, 41, REQUEST[:-1] + "0")):
            self.assertNotEqual(changed, base)
        self.assertTrue(base.startswith("start_next:li41:Ag17:"))


# ---------------------------------------------------------------------------
# 2. Engine hook (real completion path, stand-in engine, no SQL)
# ---------------------------------------------------------------------------

class SNRecorder:
    def __init__(self, capture_start_next=True, capture_eject=False):
        self.capture_start_next = capture_start_next
        self.capture_eject = capture_eject
        self.capture_requests = False
        self.offers = []
        self.snapshot_errors = 0

    def offer(self, evidence):
        self.offers.append(evidence)
        return True

    def note_snapshot_error(self):
        self.snapshot_errors += 1


class FakeFade:
    def __init__(self, volume=0.0):
        self.volume = volume

    def current_volume(self):
        return self.volume


class HookFixtures:
    def build(self):
        music, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        legal, _ = CategoryKind.objects.get_or_create(code="legal_id", defaults={"name": "Legal ID"})
        self.gold = Category.objects.create(code="SNGOLD", name="Gold", kind=music)
        self.legal = Category.objects.create(code="SNLEGAL", name="Legal", kind=legal)
        artist = Artist.objects.create(name="Start Next Artist")
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
            (LogItem.ORIGIN_REFILL_INSTALLED, self.gold),
        ]):
            track = Track.objects.create(
                title=f"Song {i}", artist=artist, category=self.gold, filepath=f"/nonexistent/sn{i}.mp3",
                duration_seconds=260.0, cue_in_seconds=2.0, next_start_seconds=250.0,
            )
            LogItem.objects.create(
                playlist_log=self.log, position=i, scheduled_time=timezone.now(), track=track,
                track_title=track.title, track_artist=artist.name, category=category, origin=origin,
            )
        self.items = list(self.log.items.select_related(*QUEUE_RELATED).order_by("position"))

    def engine(self, recorder=None, *, defer_retire=False, **state):
        engine = object.__new__(eng_module.PlaybackEngine)
        engine._lock = threading.RLock()
        engine.decks = {"A": None, "B": None}
        engine._vt = {"phase": "idle"}
        engine._start_next_active = None
        engine._start_next_restoring = None
        engine._next_triggered = True
        engine.manual_mode = False
        engine._manual_from_mic = False
        engine._manual_hold_pending = False
        engine.mic_live = False
        engine.remote_dj_session = None
        engine.dj_slots = []
        engine._engine_session_id = ENGINE
        engine._hour_refill_evidence = recorder if recorder is not None else SNRecorder()
        engine._get_deck_position = MagicMock(return_value=100.0)
        engine._start_next_track = MagicMock()
        engine._on_log_exhausted = MagicMock()
        engine._set_manual_mode = MagicMock()
        self.retired = []

        def retire(deck, on_retired=None):
            deck.retirement_started = True
            if engine.decks.get(deck.slot) is deck:
                engine.decks[deck.slot] = None
            self.retired.append((deck, on_retired))
            if on_retired is not None and not defer_retire:
                on_retired()

        engine._retire_deck_respecting_gated_seek = MagicMock(side_effect=retire)
        for name, value in state.items():
            setattr(engine, name, value)
        return engine

    def transition(self, engine, *, item_index=0, mode="fade_and_start", phase="advancing", incoming=True,
                   deck_extra=None, ownership="auto", generation=17, request_id=REQUEST):
        deck = FakeDeck("A", self.items[item_index], generation=generation, **(deck_extra or {}))
        deck.gated_seek = getattr(deck, "gated_seek", None)
        deck.volume_control = deck.mixer_pad = None
        engine.decks["A"] = deck
        incoming_deck = None
        if incoming:
            incoming_deck = FakeDeck("B", self.items[1], generation=generation + 1, occurrence_order=2)
            incoming_deck.gated_seek = None
            engine.decks["B"] = incoming_deck
        engine._start_next_active = {
            "request_id": request_id, "client_request_id": "client-press-1", "accepted_at": time.time() - 1,
            "fade_ms": 400, "user_id": 1, "mode": mode, "phase": phase, "release_manual": False,
            "accepted_monotonic": time.monotonic() - 1, "fade_ends_at": time.time(),
            "outgoing": engine._start_next_deck_identity(deck), "outgoing_deck": deck, "outgoing_ended": False,
            "incoming": {"slot": "B", "log_item_id": self.items[1].id} if incoming else None,
            "incoming_slot": "B", "incoming_attempted": True, "fade": FakeFade(0.0), "fade_owned": True,
            "fade_anchored": True, "timers": [], "paused_deck": None,
            "ownership_at_accept": engine._start_next_ownership() if ownership == "auto" else ownership,
        }
        return deck, incoming_deck

    def complete(self, engine):
        blocker = _NoQueries()
        with connection.execute_wrapper(blocker):
            engine._start_next_completion_due(engine._start_next_active["request_id"])
        self.assertEqual(blocker.calls, [])
        return engine._hour_refill_evidence

    def outcome(self, engine):
        return engine._start_next_state()["recent"][0]


class StartNextHookTests(HookFixtures, TestCase):
    def setUp(self):
        self.build()

    def test_one_completed_eligible_cut_offers_exactly_one_cause(self):
        engine = self.engine()
        deck, _incoming = self.transition(engine)
        recorder = self.complete(engine)
        (evidence,) = recorder.offers
        self.assertIsInstance(evidence, StartNextCauseEvidence)
        fields = evidence.cause_fields()
        li = self.items[0]
        self.assertEqual(fields["cause_type"], "start_next")
        self.assertEqual(fields["source_key"], start_next_source_key(ENGINE, "A", 17, li.id, REQUEST))
        self.assertEqual(fields["observed_shortening_seconds"], 150.0)  # 250 - 100: not file, not fade
        self.assertEqual(fields["original_effective_airtime_seconds"], 248.0)
        self.assertEqual(fields["start_next_position_seconds"], 100.0)
        self.assertEqual(fields["start_next_request_id"], REQUEST)
        self.assertEqual((fields["engine_instance"], fields["deck_slot"], fields["deck_generation"]),
                         (ENGINE, "A", 17))
        self.assertEqual((fields["playlist_log_id_snapshot"], fields["log_date"], fields["log_hour"],
                          fields["log_item_id_snapshot"], fields["log_item_position"]),
                         (self.log.id, date(2027, 6, 1), 14, li.id, 0))
        self.assertEqual((fields["scheduled_category_id_snapshot"], fields["scheduled_category_code"],
                          fields["scheduled_category_kind_code"]), (self.gold.id, "SNGOLD", "music"))
        self.assertEqual(fields["original_track_id_snapshot"], li.track_id)
        self.assertIsNone(fields.get("song_request_id_snapshot"))
        self.assertNotIn("eject_position_seconds", fields)
        # The transition itself completed exactly as before.
        self.assertEqual(self.outcome(engine)["outcome"], "completed")
        self.assertEqual(deck.completion_reason, "start_next")
        engine._retire_deck_respecting_gated_seek.assert_called_once()
        engine._start_next_track.assert_not_called()   # the incoming deck is live
        self.assertIsNone(engine._start_next_active)

    def test_the_cut_is_offered_only_after_the_transition_settles_completed(self):
        engine = self.engine(defer_retire=True)
        self.transition(engine)
        recorder = self.complete(engine)
        self.assertEqual(recorder.offers, [])                     # claimed, still retiring
        self.assertIn("refill_evidence", engine._start_next_active)
        (_deck, on_retired), = self.retired
        with connection.execute_wrapper(_NoQueries()):
            on_retired()
        self.assertEqual(len(recorder.offers), 1)
        self.assertEqual(self.outcome(engine)["outcome"], "completed")

    def test_a_claimed_cut_whose_transition_then_fails_offers_nothing(self):
        for label, end in [
            ("watchdog", lambda e: (e._start_next_active.__setitem__("accepted_monotonic", time.monotonic() - 99),
                                    e._start_next_watchdog())),
            ("reload", lambda e: e._start_next_cancel_for_reload()),
            ("engine stop", lambda e: e._start_next_terminal(e._start_next_active, "cancelled", "engine_stopped")),
        ]:
            with self.subTest(label):
                engine = self.engine(defer_retire=True)
                self.transition(engine)
                recorder = self.complete(engine)
                end(engine)
                self.assertIsNone(engine._start_next_active)
                (_deck, on_retired), = self.retired
                on_retired()                                       # a late retirement callback
                self.assertEqual(recorder.offers, [])
                self.assertIn(self.outcome(engine)["outcome"], ("failed", "cancelled"))

    def test_capture_off_costs_nothing_and_reads_no_position(self):
        engine = self.engine(SNRecorder(capture_start_next=False, capture_eject=True))
        self.transition(engine)
        recorder = self.complete(engine)
        self.assertEqual(recorder.offers, [])
        engine._get_deck_position.assert_not_called()
        self.assertEqual(self.outcome(engine)["outcome"], "completed")

    def test_no_recorder_is_harmless(self):
        engine = self.engine()
        del engine._hour_refill_evidence
        self.transition(engine)
        engine._start_next_completion_due(REQUEST)
        self.assertEqual(self.outcome(engine)["outcome"], "completed")

    def assert_no_cause(self, engine, outcome="completed"):
        recorder = self.complete(engine)
        self.assertEqual(recorder.offers, [])
        self.assertEqual(recorder.snapshot_errors, 0)
        if outcome is not None:
            self.assertEqual(self.outcome(engine)["outcome"], outcome)
        return recorder

    def test_overlap_and_to_silence_are_never_causes(self):
        engine = self.engine()
        self.transition(engine, mode="overlap")
        self.assert_no_cause(engine)
        engine = self.engine()
        self.transition(engine, phase="to_silence", incoming=False)
        self.assert_no_cause(engine, outcome="completed_to_silence")
        engine._on_log_exhausted.assert_called_once_with("A")   # end-of-queue authority unchanged

    def test_ownership_exclusions_at_accept_and_at_the_cut(self):
        at_cut = {
            "manual": dict(manual_mode=True),
            "remote dj": dict(remote_dj_session=SimpleNamespace(slot_id=None, connection_attempt=None)),
            "local mic": dict(mic_live=True),
            "voice track": dict(_vt={"phase": "outro_playing", "outgoing_track_id": -1}),
        }
        for label, state in at_cut.items():
            with self.subTest(f"{label} at the cut"):
                engine = self.engine()
                self.transition(engine)
                for name, value in state.items():
                    setattr(engine, name, value)
                self.assert_no_cause(engine, outcome=None)
        for key in ("manual", "remote_dj", "mic"):
            with self.subTest(f"{key} at accept"):
                engine = self.engine()
                ownership = {"manual": False, "remote_dj": False, "mic": False, key: True}
                self.transition(engine, ownership=ownership)
                self.assert_no_cause(engine)
        with self.subTest("ownership unreadable at accept"):
            engine = self.engine()
            self.transition(engine, ownership=None)
            self.assert_no_cause(engine)

    def test_deck_state_exclusions(self):
        cases = {
            "not yet on air": dict(air_start_recorded=False),
            "seek continuation": dict(continuation_reason="manual_seek_or_resume"),
            "restart resume": dict(continuation_reason="auto_resume"),
            "already started replay": dict(continuation_reason="already_started_occurrence"),
            "gated seek": dict(gated_seek={"phase": "confirming"}),
        }
        for label, extra in cases.items():
            with self.subTest(label):
                engine = self.engine()
                self.transition(engine, deck_extra=extra)
                if "gated_seek" in extra:
                    engine._retire_deck_respecting_gated_seek.side_effect = None
                    self.assert_no_cause(engine, outcome=None)
                else:
                    self.assert_no_cause(engine)

    def test_only_generated_schedule_music_qualifies(self):
        for index, label in [(2, "operator inserted"), (3, "live fill"), (4, "engine inserted (dedication)"),
                             (5, "build-time fill"), (6, "unknown/legacy"), (7, "legal id slot"),
                             (8, "refill installed")]:
            with self.subTest(label):
                engine = self.engine()
                self.transition(engine, item_index=index)
                self.assert_no_cause(engine)

    def test_a_foreign_live_deck_is_not_a_cause(self):
        engine = self.engine()
        _deck, incoming = self.transition(engine, incoming=False, phase="advancing")
        engine._start_next_active["incoming"] = {"slot": "B", "log_item_id": self.items[1].id}
        engine._start_next_active["incoming_slot"] = "A"   # the live B deck is not this transition's
        engine.decks["B"] = FakeDeck("B", self.items[1], generation=30, occurrence_order=2)
        self.assert_no_cause(engine)

    def test_a_lost_claim_is_never_a_cause_and_never_completes_this_path(self):
        engine = self.engine()
        deck, _ = self.transition(engine, deck_extra=dict(completion_claimed=True, completion_reason="eos"))
        recorder = self.complete(engine)
        self.assertEqual(recorder.offers, [])
        engine._retire_deck_respecting_gated_seek.assert_not_called()
        self.assertIsNotNone(engine._start_next_active)   # EOS owns the deck (_start_next_outgoing_ended)

    def test_a_late_completion_timer_after_the_cut_adds_nothing(self):
        engine = self.engine(defer_retire=True)
        self.transition(engine)
        recorder = self.complete(engine)
        self.complete(engine)                               # outgoing_ended: returns at once
        (_deck, on_retired), = self.retired
        on_retired()
        engine._start_next_completion_due(REQUEST)          # transition gone: returns at once
        self.assertEqual(len(recorder.offers), 1)

    def test_no_remaining_airtime_and_reading_failures(self):
        engine = self.engine(_get_deck_position=MagicMock(return_value=250.0))
        self.transition(engine)
        self.assert_no_cause(engine)
        engine = self.engine(_get_deck_position=MagicMock(side_effect=RuntimeError("gst")))
        deck, _ = self.transition(engine)
        recorder = self.complete(engine)
        self.assertEqual((recorder.offers, recorder.snapshot_errors), ([], 1))
        self.assertEqual(self.outcome(engine)["outcome"], "completed")   # playout unaffected
        self.assertEqual(deck.completion_reason, "start_next")

    def test_resumed_item_without_cached_relations_fails_closed_without_a_query(self):
        resumed = LogItem.objects.select_related("track").get(pk=self.items[0].pk)
        self.items[0] = resumed
        engine = self.engine()
        self.transition(engine)
        self.assert_no_cause(engine)

    def test_an_eject_racing_the_transition(self):
        # Eject first: the transition is superseded before any claim, and the
        # Eject itself is refused as a cause (start_next_transition, r0120).
        recorder = SNRecorder(capture_eject=True)
        engine = self.engine(recorder)
        deck, _ = self.transition(engine)
        with connection.execute_wrapper(_NoQueries()):
            engine._eject_deck("A", expected_log_item_id=self.items[0].id)
        self.assertEqual(self.outcome(engine)["reason"], "superseded_by_eject")
        self.assertEqual(deck.completion_reason, "eject")
        self.assertEqual(recorder.offers, [])
        # Cut first, Eject while the generation is still retiring (only
        # possible with a deferred retirement): the Eject loses its claim AND
        # supersedes the unsettled transition, so neither records a cause.
        recorder = SNRecorder(capture_eject=True)
        engine = self.engine(recorder, defer_retire=True)
        deck, _ = self.transition(engine)
        self.complete(engine)
        engine.decks["A"] = deck                            # still mapped while it retires
        deck.retirement_started = False
        with connection.execute_wrapper(_NoQueries()):
            engine._eject_deck("A", expected_log_item_id=self.items[0].id)
        self.assertEqual(deck.completion_reason, "start_next")
        self.assertEqual(self.outcome(engine)["reason"], "superseded_by_eject")
        (_d, on_retired), = self.retired[:1]
        on_retired()
        self.assertEqual(recorder.offers, [])

    def test_acceptance_records_ownership_and_nothing_else_changes(self):
        engine = self.engine()
        self.assertEqual(engine._start_next_ownership(), {"manual": False, "remote_dj": False, "mic": False})
        engine.manual_mode = True
        self.assertEqual(engine._start_next_ownership()["manual"], True)
        broken = object.__new__(eng_module.PlaybackEngine)   # missing attributes: never raises
        self.assertIsNone(broken._start_next_ownership())
        engine = self.engine()
        self.transition(engine)
        published = engine._start_next_state()["active"]
        self.assertNotIn("ownership_at_accept", published)
        self.assertNotIn("refill_evidence", published)


# ---------------------------------------------------------------------------
# 3. Real Start Next transport (GStreamer)
# ---------------------------------------------------------------------------

class StartNextTransportTests(TestCase):
    """The real state machine on real GStreamer decks; real saved rows loaded
    as the engine's queue loads them, so the real snapshot classifies them.
    Opening a deck reads the database (spoken-projection authority), so the
    zero-query proof is on the evidence snapshot itself: it runs under a
    blocker that fails on ANY query."""

    def setUp(self):
        from library.tests._transport_harness import TransportHarness, patch_voicetracks

        self.h = TransportHarness(self)
        self.engine = self.h.engine
        patch_voicetracks(self)
        self.recorder = SNRecorder()
        self.engine._hour_refill_evidence = self.recorder
        # The harness skips the DB air-start write; mark first audio the way
        # its committed transaction would.
        self.engine._schedule_occurrence_air_start_from_probe = (
            lambda deck: setattr(deck, "air_start_recorded", True))
        music, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.category = Category.objects.create(code="SN_GOLD", name="Gold", kind=music)
        self.artist = Artist.objects.create(name="Harness Artist")
        self.hour = 0
        self.snapshots = []
        real = eng_module.start_next_evidence_from_engine

        def spy(engine, deck, active, *, occurred_at):
            blocker = _NoQueries()
            with connection.execute_wrapper(blocker):
                result = real(engine, deck, active, occurred_at=occurred_at)
            self.assertEqual(blocker.calls, [])
            self.snapshots.append((deck.completion_reason, active["mode"], result[1]))
            return result

        for patcher in (
            patch.object(eng_module, "start_next_evidence_from_engine", side_effect=spy),
            # Deck open recycles the thread's connection (production); inside
            # this TestCase's transaction that would close it.
            patch.object(eng_module, "close_old_connections", new=lambda: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def queue(self, *specs, origin=LogItem.ORIGIN_GENERATED_SCHEDULE):
        """Saved rows carrying the harness media; returns their ids."""
        mocks = self.h.queue(*specs)
        log = PlaylistLog.objects.create(date=date(2027, 6, 1), hour=self.hour, status="approved")
        self.hour += 1
        for position, mock in enumerate(mocks):
            t = mock.track
            track, _ = Track.objects.get_or_create(filepath=t.filepath, defaults=dict(   # media is shared
                filename=t.filepath.rsplit("/", 1)[-1], format="wav", title=t.title,
                artist=self.artist, category=self.category, duration_seconds=t.duration_seconds,
                next_start_seconds=t.next_start_seconds, cue_in_seconds=t.cue_in_seconds,
            ))
            LogItem.objects.create(
                playlist_log=log, position=position, scheduled_time=timezone.now(), track=track,
                track_title=track.title, track_artist=self.artist.name, category=self.category, origin=origin,
            )
        rows = list(log.items.select_related(*QUEUE_RELATED).order_by("position"))
        self.engine.log_items = rows
        self.engine._queue_cursor = 0
        return [row.id for row in rows]

    def send(self, **kwargs):
        from library.tests._transport_harness import start_next_command

        command = start_next_command(self.engine, **kwargs)
        self.engine._dispatch_engine_command(command)
        return command

    def settle(self, timeout=6.0):
        from library.tests._transport_harness import wait_until

        self.assertTrue(wait_until(lambda: self.engine._start_next_active is None, timeout),
                        self.engine._start_next_state())
        return self.engine._start_next_state()["recent"][0]

    def start(self, slot="A", seconds=0.6):
        from library.tests._transport_harness import pump, wait_until

        self.engine._start_next_track(slot=slot)
        pump(seconds)
        self.assertTrue(wait_until(lambda: self.engine.decks[slot].air_start_recorded, 2.0))
        return self.engine.decks[slot]

    def three(self, first_seconds=8):
        return self.queue({"id": 1, "seconds": first_seconds}, {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8})

    def test_a_completed_fade_and_start_is_exactly_one_cause_with_the_real_position(self):
        ids = self.three()
        self.h.play()
        deck = self.start()
        at_press = self.engine._get_deck_position(deck)
        command = self.send(fade_ms=400)
        record = self.settle()
        self.assertEqual((record["outcome"], record["mode"]), ("completed", "fade_and_start"))
        self.assertEqual(self.snapshots, [("start_next", "fade_and_start", None)])
        (evidence,) = self.recorder.offers
        fields = evidence.cause_fields()
        self.assertEqual((fields["log_item_id_snapshot"], fields["deck_slot"], fields["deck_generation"]),
                         (ids[0], "A", deck.generation))
        self.assertEqual(fields["start_next_request_id"], command["request_id"])
        self.assertEqual(fields["engine_instance"], "harness-engine-instance")
        position = fields["start_next_position_seconds"]
        # The deck's own source position at the claim: one fade after the
        # press (the faded seconds aired), never the file or press time.
        self.assertGreaterEqual(position - at_press, 0.4 - 0.05)
        self.assertLess(position - at_press, 0.4 + 1.0)
        self.assertAlmostEqual(fields["observed_shortening_seconds"], round(8.0 - position, 3), places=3)
        self.assertEqual(self.h.claims, ids[:2])             # playback unchanged

    def test_a_longer_fade_moves_the_cut_point_not_the_arithmetic(self):
        ids = self.three()
        self.h.play()
        deck = self.start()
        at_press = self.engine._get_deck_position(deck)
        self.send(fade_ms=2000)
        self.assertEqual(self.settle()["outcome"], "completed")
        (evidence,) = self.recorder.offers
        fields = evidence.cause_fields()
        self.assertGreaterEqual(fields["start_next_position_seconds"] - at_press, 2.0 - 0.05)
        self.assertLess(fields["start_next_position_seconds"] - at_press, 2.0 + 1.0)
        self.assertAlmostEqual(fields["observed_shortening_seconds"],
                               round(8.0 - fields["start_next_position_seconds"], 3), places=3)
        self.assertEqual(self.h.claims, ids[:2])

    def test_a_repeated_command_file_and_a_repeated_press_add_nothing(self):
        ids = self.three()
        self.h.play()
        self.start()
        command = self.send()
        self.settle()
        self.engine._dispatch_engine_command(dict(command))                       # same file again
        self.engine._dispatch_engine_command(dict(command, request_id="another-request-id"))  # same press
        self.assertEqual(self.engine._start_next_state()["recent"][0]["reason"], "duplicate_request")
        self.assertEqual(len(self.recorder.offers), 1)
        self.assertEqual(self.h.claims, ids[:2])

    def test_overlap_is_not_a_cause(self):
        ids = self.three()
        self.h.play()
        self.start("A", 0.4)
        self.start("B", 0.4)
        self.send()
        record = self.settle()
        self.assertEqual((record["outcome"], record["mode"]), ("completed", "overlap"))
        self.assertEqual(self.snapshots, [("start_next", "overlap", "not_fade_and_start")])
        self.assertEqual(self.recorder.offers, [])
        self.assertEqual(self.h.claims, ids[:2])

    def test_start_only_from_a_paused_deck_is_not_a_cause(self):
        from library.tests._transport_harness import pump

        self.three()
        self.h.play()
        self.start()
        self.engine._pause_deck("A")
        self.send(expected={"live_decks": []})
        self.assertEqual(self.settle()["mode"], "start_only")
        pump(0.3)
        self.assertEqual((self.snapshots, self.recorder.offers), ([], []))   # hook is on the fade path only

    def test_fade_to_silence_is_not_a_cause(self):
        ids = self.queue({"id": 1, "seconds": 8})
        self.h.play()
        self.start()
        self.send()
        self.assertEqual(self.settle()["outcome"], "completed_to_silence")
        self.assertEqual(self.snapshots, [("start_next", "fade_and_start", "to_silence")])
        self.assertEqual(self.recorder.offers, [])
        self.assertEqual(self.h.on_log_exhausted_calls, ["A"])   # end-of-queue authority unchanged
        self.assertEqual(self.h.claims, ids)

    def test_eos_before_the_fade_ends_is_not_a_cause(self):
        ids = self.three(first_seconds=1.0)
        self.h.play()
        self.start(seconds=0.3)
        self.send(fade_ms=3000)
        self.assertEqual(self.settle()["outcome"], "completed")
        self.assertEqual(self.snapshots, [])                # no start_next claim ever happened
        self.assertEqual(self.recorder.offers, [])
        self.assertEqual(self.h.claims, ids[:2])

    def test_eject_during_the_fade_cancels_and_records_nothing(self):
        from library.tests._transport_harness import pump

        self.recorder.capture_eject = True
        ids = self.three()
        self.h.play()
        self.start()
        self.send(fade_ms=1500)
        pump(0.2)
        self.engine._eject_deck("A")
        self.assertEqual(self.engine._start_next_state()["recent"][0]["reason"], "superseded_by_eject")
        pump(2.0)
        self.assertEqual(self.snapshots, [])
        self.assertEqual(self.recorder.offers, [])          # neither a Start Next nor an Eject cause
        self.assertEqual(self.h.claims, ids[:2])

    def test_an_unopenable_incoming_fails_and_records_nothing(self):
        ids = self.three()
        original = self.engine._claim_playback_occurrence
        self.engine._claim_playback_occurrence = (
            lambda item: False if item.id in ids[1:] else original(item))
        self.h.play()
        self.start()
        self.send(fade_ms=800)
        record = self.settle()
        self.assertEqual((record["outcome"], record["reason"]), ("failed", "incoming_open_failed"))
        self.assertEqual((self.snapshots, self.recorder.offers), ([], []))

    def test_manual_capture_off_and_inserted_items_are_not_causes(self):
        cases = {
            "manual": (dict(), dict(manual_mode=True), "manual_hold"),
            "capture off": (dict(), dict(), None),
            "inserted": (dict(origin=LogItem.ORIGIN_OPERATOR_INSERTED), dict(), "not_scheduled_occurrence"),
        }
        for label, (queue_kwargs, engine_state, reason) in cases.items():
            with self.subTest(label):
                from library.tests._transport_harness import pump

                for deck in [d for d in self.engine.decks.values() if d is not None]:
                    self.engine._eject_deck(deck.slot)
                pump(0.4)
                self.recorder.offers.clear()
                self.snapshots.clear()
                self.queue({"id": 1, "seconds": 8}, {"id": 2, "seconds": 8}, **queue_kwargs)
                self.engine.manual_mode = engine_state.get("manual_mode", False)
                self.recorder.capture_start_next = label != "capture off"
                self.h.play()
                self.start()
                self.send()
                self.assertEqual(self.settle()["outcome"], "completed")
                self.assertEqual(self.recorder.offers, [])
                expected = [] if reason is None else [("start_next", "fade_and_start", reason)]
                self.assertEqual(self.snapshots, expected)
        self.engine.manual_mode = False

    def test_a_stale_or_mistargeted_command_is_refused_and_records_nothing(self):
        ids = self.three()
        self.h.play()
        deck = self.start()
        self.send(issued_at=time.time() - 3600)
        self.assertEqual(self.engine._start_next_state()["recent"][0]["reason"], "stale_command")
        self.send(expected={"live_decks": [{"slot": "A", "log_item_id": ids[0], "generation": deck.generation + 9}]})
        self.assertEqual(self.engine._start_next_state()["recent"][0]["reason"], "target_changed")
        self.send(engine_instance="old-engine")
        self.assertEqual(self.engine._start_next_state()["recent"][0]["reason"], "engine_restarted")
        self.assertEqual((self.snapshots, self.recorder.offers), ([], []))
        self.assertEqual(self.h.claims, ids[:1])


# ---------------------------------------------------------------------------
# 4. Evidence worker
# ---------------------------------------------------------------------------

def _start_next_evidence(n=1, generation=17, *, request=REQUEST, log_item_id=None):
    li = log_item_id or 9000 + n
    env = StartNextCauseEnvelope(
        source_key=start_next_source_key(ENGINE, "A", generation, li, request), engine_instance=ENGINE,
        request_id=request, slot="A", deck_generation=generation, log_item_id=li,
        cut_position_seconds=100.0, remaining_effective_airtime_seconds=150.0,
    )
    return StartNextCauseEvidence(
        envelope=env, occurred_at=timezone.now(), playlist_log_id=77, log_date=date(2027, 6, 1), log_hour=14,
        log_item_position=n, scheduled_category_id=5, scheduled_category_code="GOLD",
        scheduled_category_kind_code="music", original_track_id=500 + n, original_track_title="Song",
        original_track_artist="Artist", original_effective_airtime_seconds=248.0,
    )


def _eject_evidence(n=1):
    env = EjectCauseEnvelope(
        source_key=f"operator_eject:{ENGINE}:A:g{n}:li{8000 + n}", engine_instance=ENGINE, slot="A",
        deck_generation=n, log_item_id=8000 + n, eject_position_seconds=100.0,
        remaining_effective_airtime_seconds=150.0,
    )
    return EjectCauseEvidence(
        envelope=env, occurred_at=timezone.now(), playlist_log_id=77, log_date=date(2027, 6, 1), log_hour=14,
        log_item_position=n, scheduled_category_id=5, scheduled_category_code="GOLD",
        scheduled_category_kind_code="music", original_track_id=600 + n, original_track_title="Song",
        original_track_artist="Artist", original_effective_airtime_seconds=248.0,
    )


class _Unknown:
    source_key = "bogus:1"

    def cause_fields(self):
        return {"cause_type": "operator_skip", "source_key": self.source_key}


class StartNextWorkerTests(TransactionTestCase):
    def recorder(self, flags, **kwargs):
        recorder = HourRefillEvidenceRecorder(read_flags=lambda: flags, policy_refresh_seconds=60, **kwargs)
        recorder.start()
        self.addCleanup(recorder.stop)
        self.assertTrue(recorder.wait_until_ready(5))
        return recorder

    def drain(self, recorder, count_key, expected):
        self.assertTrue(_wait(lambda: recorder.counters[count_key] >= expected), recorder.counters)

    def test_the_start_next_switch_is_independent(self):
        cases = [
            ((True, True, False), {"start_next": 0, "eject": 1}),
            ((False, False, True), {"start_next": 1, "eject": 0}),
            ((True, True, True), {"start_next": 1, "eject": 1}),
        ]
        for flags, expected in cases:
            with self.subTest(flags):
                HourRefillCause.objects.all().delete()
                recorder = self.recorder(flags)
                self.assertEqual(recorder.capture_start_next, flags[2])
                recorder.offer(_start_next_evidence())
                recorder.offer(_eject_evidence())
                self.drain(recorder, "recorded", sum(expected.values()))
                self.drain(recorder, "dropped_policy_off", 2 - sum(expected.values()))
                self.assertEqual(HourRefillCause.objects.filter(cause_type="start_next").count(),
                                 expected["start_next"])
                self.assertEqual(HourRefillCause.objects.filter(cause_type="operator_eject").count(),
                                 expected["eject"])
                recorder.stop()

    def test_an_unknown_type_is_dropped_even_with_every_switch_on(self):
        inserted = []
        recorder = self.recorder((True, True, True), insert=inserted.append)
        recorder.offer(_Unknown())
        self.drain(recorder, "dropped_unknown_type", 1)
        self.assertEqual(inserted, [])
        self.assertEqual(recorder.counters["recorded"], 0)

    def test_a_two_value_reader_leaves_start_next_off(self):
        recorder = self.recorder((True, True))
        self.assertEqual((recorder.capture_requests, recorder.capture_eject, recorder.capture_start_next),
                         (True, True, False))
        recorder.offer(_start_next_evidence())
        self.drain(recorder, "dropped_policy_off", 1)
        self.assertFalse(HourRefillCause.objects.exists())

    def test_a_duplicate_cut_inserts_once(self):
        recorder = self.recorder((False, False, True))
        recorder.offer(_start_next_evidence())
        recorder.offer(_start_next_evidence())
        self.drain(recorder, "duplicate", 1)
        self.assertEqual(HourRefillCause.objects.count(), 1)

    def test_the_default_reader_uses_the_policy_row_and_fails_closed(self):
        recorder = HourRefillEvidenceRecorder()
        self.assertEqual(recorder._refresh_policy(), (False, False))
        self.assertFalse(recorder.capture_start_next)                 # no row: Off
        HourRefillPolicy(enabled=True, capture_start_next=True).save()
        recorder._refresh_policy()
        self.assertEqual((recorder.capture_requests, recorder.capture_eject, recorder.capture_start_next),
                         (False, False, True))
        HourRefillPolicy.objects.filter(pk=1).update(enabled=False)
        recorder._refresh_policy()
        self.assertFalse(recorder.capture_start_next)                 # the master switch gates it
        with patch.object(HourRefillPolicy, "capture_switches", side_effect=RuntimeError("db")):
            HourRefillPolicy.objects.filter(pk=1).update(enabled=True)
            recorder._refresh_policy()
        self.assertFalse(recorder.capture_start_next)                 # unreadable: Off

    def test_a_worker_insert_failure_never_reaches_the_engine(self):
        def broken(_evidence):
            raise RuntimeError("db down")

        recorder = self.recorder((False, False, True), insert=broken)
        self.assertTrue(recorder.offer(_start_next_evidence()))
        self.drain(recorder, "dropped_error", 1)


# ---------------------------------------------------------------------------
# 5. Schema and migration
# ---------------------------------------------------------------------------

class StartNextSchemaTests(TestCase):
    def fields(self, **overrides):
        values = _start_next_evidence().cause_fields()
        values.update(overrides)
        return values

    def test_a_complete_start_next_row_is_accepted_and_append_only(self):
        row = HourRefillCause.objects.create(**self.fields())
        self.assertEqual(row.cause_type, "start_next")
        with self.assertRaises(ValueError):
            row.save()

    def test_checks_reject_incomplete_identity_and_unknown_types(self):
        bad = {
            "no generation": dict(deck_generation=None),
            "no position": dict(start_next_position_seconds=None),
            "no track": dict(original_track_id_snapshot=None),
            "no engine": dict(engine_instance=""),
            "no slot": dict(deck_slot=""),
            "no request": dict(start_next_request_id=""),
            "zero shortening": dict(observed_shortening_seconds=0.0),
            "unknown type": dict(cause_type="operator_skip"),
        }
        for label, overrides in bad.items():
            with self.subTest(label), self.assertRaises(IntegrityError), transaction.atomic():
                HourRefillCause.objects.create(**self.fields(source_key=f"k-{label}", **overrides))

    def test_r0120_rows_remain_valid(self):
        HourRefillCause.objects.create(**_eject_evidence().cause_fields())
        HourRefillCause.objects.create(
            source_key="request_substitution:li1:req1:t1-2", cause_type=CAUSE_REQUEST_SUBSTITUTION,
            occurred_at=timezone.now(), playlist_log_id_snapshot=1, log_date=date(2027, 6, 1), log_hour=14,
            log_item_id_snapshot=1, log_item_position=0, original_effective_airtime_seconds=200.0,
            original_track_id_snapshot=1, replacement_track_id_snapshot=2,
            replacement_effective_airtime_seconds=150.0, observed_shortening_seconds=50.0,
            song_request_id_snapshot=1,
        )
        self.assertEqual(HourRefillCause.objects.filter(start_next_request_id="").count(), 2)

    def test_policy_defaults_off_and_the_r0120_flag_shape_is_unchanged(self):
        self.assertEqual(HourRefillPolicy.capture_flags(), (False, False))
        self.assertEqual(HourRefillPolicy.capture_switches(), (False, False, False))
        policy = HourRefillPolicy()
        self.assertFalse(policy.capture_start_next)
        HourRefillPolicy(enabled=True, capture_start_next=True).save()
        self.assertEqual(HourRefillPolicy.capture_flags(), (False, False))
        self.assertEqual(HourRefillPolicy.capture_switches(), (False, False, True))

    def test_0097_operations_and_update_center_classification(self):
        plan = _serialize_exact_plan(MigrationLoader(connection), ["library.0097_hour_refill_start_next_cause"])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[0]["operations"]], [
            ("AddField", "additive"), ("AddField", "additive"), ("AddField", "additive"),
            ("AlterField", "additive"),
            ("RemoveConstraint", "manual"), ("AddConstraint", "manual"), ("AddConstraint", "manual"),
        ])

    def test_0097_writes_no_data_and_enables_nothing(self):
        from pathlib import Path

        source = (Path(settings.BASE_DIR) / "library/migrations/0097_hour_refill_start_next_cause.py").read_text()
        body = source.split("operations = [")[1]
        for forbidden in ("RunPython", "RunSQL", "update(", "objects", "default=True"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, body)


class StartNextMigrationTests(TransactionTestCase):
    """Old schema (0096) vs new (0097): r0120 rows survive, new code fails
    closed on the old schema, reverse refuses while start_next rows exist."""

    def tearDown(self):
        call_command("migrate", verbosity=0)

    def test_forward_reverse_and_old_schema_fail_closed(self):
        from django.db import connection as conn

        call_command("migrate", "library", "0096", verbosity=0)
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO library_hourrefillpolicy (id, enabled, capture_request_substitution, "
                "capture_operator_eject, dry_run_only, updated_at) VALUES (1, true, true, true, false, now())")
        with ARMED:
            self.assertFalse(inst.install_permitted())                       # new code, old schema
            self.assertFalse(inst.install_permitted(cause_types=(CAUSE_OPERATOR_EJECT,)))
        self.assertEqual(runner_mod.read_policy_state().reason, "policy_unreadable")
        recorder = HourRefillEvidenceRecorder()
        self.assertEqual(recorder._refresh_policy(), (False, False))
        self.assertFalse(recorder.capture_start_next)
        self.assertEqual(HourRefillPolicy.capture_flags(), (True, True))     # r0120 reader still works
        # r0121 automatic causes: this code also reads 0098's columns, so "new
        # schema" is the latest library migration, not 0097 itself.
        call_command("migrate", "library", verbosity=0)
        self.assertEqual(HourRefillPolicy.capture_switches(), (True, True, False))
        with ARMED:
            self.assertTrue(inst.install_permitted(cause_types=(CAUSE_OPERATOR_EJECT,)))
            self.assertFalse(inst.install_permitted(cause_types=(CAUSE_START_NEXT,)))
        HourRefillCause.objects.create(**_start_next_evidence().cause_fields())
        with self.assertRaises(IntegrityError):
            call_command("migrate", "library", "0096", verbosity=0)
        HourRefillCause.objects.filter(cause_type=CAUSE_START_NEXT).delete()
        call_command("migrate", "library", "0096", verbosity=0)
        with conn.cursor() as cursor:
            cursor.execute("SELECT enabled, capture_operator_eject FROM library_hourrefillpolicy WHERE id = 1")
            self.assertEqual(cursor.fetchone(), (True, True))


# ---------------------------------------------------------------------------
# 6. Trace, projection, thresholds, policy gates
# ---------------------------------------------------------------------------

def cut(cause_id, log_item_id, *, shortening=120.0, minute=30, track=None, log=LOG, hour=14, category=GOLD):
    return CauseRecord(
        id=cause_id, source_key=start_next_source_key("eng", "A", cause_id, log_item_id, f"req{cause_id}"),
        cause_type=CAUSE_START_NEXT, occurred_at=at(14, minute), log_id=log, log_date=date(2027, 6, 1),
        log_hour=hour, log_item_id=log_item_id, log_item_position=log_item_id,
        scheduled_category_id=category, scheduled_category_code=f"C{category}",
        scheduled_category_kind_code="music",
        original_track_id=1000 + log_item_id if track is None else track,
        original_effective_airtime_seconds=240.0, replacement_track_id=None,
        replacement_effective_airtime_seconds=None, observed_shortening_seconds=shortening,
        engine_instance="eng", deck_slot="A", deck_generation=cause_id, start_next_request_id=f"req{cause_id}",
        start_next_position_seconds=120.0,
    )


def window(future_seconds, *, deck_position=10.0):
    """14:40 -> 15:00 = 1200 s: items 1, 2 played; 3 on deck (240 s)."""
    played = [item(1, played=True, claimed=True), item(2, played=True, claimed=True)]
    on_deck = item(3, 240.0, played=True, claimed=True)
    future = [item(4 + i, s) for i, s in enumerate(future_seconds)]
    return snapshot(played + [on_deck] + future, 3, [deck_state(on_deck, deck_position)])


class StartNextProjectionTests(SimpleTestCase):
    def project(self, snap, causes):
        return proj.project_refill(snap, causes=causes, boundary=boundary(), allowance=0)

    def test_only_a_finished_identical_occurrence_qualifies(self):
        snap = window([290.0, 290.0])
        cases = {
            "finished": (cut(1, 2), None),
            "on the deck": (cut(1, 3), "occurrence_still_pending"),
            "still queued": (cut(1, 4), "occurrence_still_pending"),
            "other track now": (cut(1, 2, track=5555), "superseded"),
            "other log": (cut(1, 2, log=LOG + 1), "stale_log"),
            "other hour": (cut(1, 2, hour=13), "other_hour"),
            "missing occurrence": (cut(1, 99), "missing_occurrence"),
        }
        for label, (cause, reason) in cases.items():
            with self.subTest(label):
                qualified, excluded = proj.partition_causes(snap, [cause])
                if reason is None:
                    self.assertEqual([c.cause_type for c in qualified], ["start_next"])
                else:
                    self.assertEqual((qualified, excluded), ((), ((cause.source_key, reason),)))

    def test_net_deficit_not_the_observed_shortening_decides(self):
        # window 1200; runway 230 (deck) + 290 + 290 = 810 -> net 390.
        p = self.project(window([290.0, 290.0]), [cut(1, 2, shortening=5.0)])
        self.assertEqual((p.outcome, p.net_shortage_seconds, p.observed_shortening_seconds), ("eligible", 390.0, 5.0))
        self.assertEqual((p.threshold_seconds, p.threshold_basis), (30.0, "start_next_only_provisional"))
        # A huge observed cut with the hour already full: no install.
        full = self.project(window([290.0, 290.0, 290.0, 290.0]), [cut(1, 2, shortening=200.0)])
        self.assertEqual(full.outcome, "already_satisfied")

    def test_thresholds_are_strict_and_mixed_sets_take_the_strictest(self):
        snap_for = lambda net: window([1200.0 - 230.0 - net])                      # noqa: E731
        cases = [
            ([cut(1, 2)], 30.0, "below_threshold", "start_next_only_provisional"),
            ([cut(1, 2)], 30.001, "eligible", "start_next_only_provisional"),
            ([cut(1, 2), eject(2, 1)], 30.0, "below_threshold", "operator_cuts_provisional"),
            ([cut(1, 2), eject(2, 1)], 30.5, "eligible", "operator_cuts_provisional"),
            ([cut(1, 2), swap(3, 4, 1004)], 60.0, "below_threshold", "request_substitution_present"),
            ([cut(1, 2), swap(3, 4, 1004)], 60.5, "eligible", "request_substitution_present"),
            ([eject(2, 1)], 30.0, "below_threshold", "operator_eject_only_provisional"),   # r0120 unchanged
        ]
        for causes, net, outcome, basis in cases:
            with self.subTest(types=[c.cause_type for c in causes], net=net):
                p = self.project(snap_for(net), causes)
                self.assertEqual((p.outcome, p.threshold_basis), (outcome, basis))
                self.assertEqual(p.net_shortage_seconds, net)

    def test_threshold_for_never_relaxes_for_an_unknown_type(self):
        unknown = SimpleNamespace(cause_type="operator_skip")
        self.assertEqual(proj.threshold_for([unknown]), (60.0, "unknown_cause_strictest"))
        self.assertEqual(proj.threshold_for([]), (30.0, "operator_eject_only_provisional"))

    def test_one_cut_per_occurrence_is_summed(self):
        trace = build_causality_trace([eject(1, 2, shortening=100.0), cut(2, 2, shortening=90.0, minute=31)],
                                      active_log_date=date(2027, 6, 1), active_log_hour=14)
        self.assertEqual([r.cause_type for r in trace.causes], ["operator_eject"])
        self.assertEqual(trace.duplicates, ((cut(2, 2).source_key, "duplicate_occurrence_cause"),))
        self.assertEqual(trace.observed_shortening_seconds, 100.0)
        # A request swap and a later cut of the same occurrence are different seconds.
        trace = build_causality_trace([swap(1, 2, 1002, shortening=60.0), cut(2, 2, shortening=90.0, minute=32)],
                                      active_log_date=date(2027, 6, 1), active_log_hour=14)
        self.assertEqual(trace.observed_shortening_seconds, 150.0)
        # The same key twice (a duplicate row) counts once.
        trace = build_causality_trace([cut(1, 2), cut(1, 2)], active_log_date=date(2027, 6, 1), active_log_hour=14)
        self.assertEqual(len(trace.causes), 1)

    def test_transport_still_blocks_during_start_next_and_without_a_live_deck(self):
        snap = window([290.0])
        for flags, reason in [(dict(start_next_active=True), "start_next"),
                              (dict(next_triggered=True), "transition_pending")]:
            with self.subTest(reason):
                p = self.project(replace(snap, **flags), [cut(1, 2)])
                self.assertEqual((p.outcome, p.reasons), ("blocked_transport", (reason,)))
        silent = replace(snap, decks=())
        p = self.project(silent, [cut(1, 2)])
        self.assertEqual((p.outcome, p.reasons), ("unsafe_window", ("no_live_deck",)))

    def test_policy_state_allows_each_type_only_with_its_own_switch(self):
        state = runner_mod.PolicyState("observation", "x", request_cause=True, eject_cause=True)
        self.assertFalse(state.allows([CAUSE_START_NEXT]))
        self.assertFalse(state.allows([CAUSE_OPERATOR_EJECT, CAUSE_START_NEXT]))
        self.assertTrue(state.allows([CAUSE_OPERATOR_EJECT, CAUSE_REQUEST_SUBSTITUTION]))
        state = runner_mod.PolicyState("observation", "x", start_next_cause=True)
        self.assertTrue(state.allows([CAUSE_START_NEXT]))
        self.assertFalse(state.allows([CAUSE_START_NEXT, CAUSE_OPERATOR_EJECT]))
        self.assertFalse(state.allows(["operator_skip"]))
        self.assertEqual(state.as_dict()["start_next_cause"], True)


class StartNextPolicyGateTests(TestCase):
    def test_runner_policy_reads_the_new_switch(self):
        self.assertEqual(runner_mod.read_policy_state().reason, "no_policy_row")
        HourRefillPolicy(enabled=True, capture_start_next=True).save()
        state = runner_mod.read_policy_state()
        self.assertEqual((state.mode, state.start_next_cause, state.eject_cause), ("observation", True, False))

    def test_install_permitted_needs_every_participating_switch(self):
        HourRefillPolicy(enabled=True, capture_operator_eject=True, dry_run_only=False).save()
        with ARMED:
            self.assertTrue(inst.install_permitted(cause_types=(CAUSE_OPERATOR_EJECT,)))
            self.assertFalse(inst.install_permitted(cause_types=(CAUSE_START_NEXT,)))
            self.assertFalse(inst.install_permitted(cause_types=(CAUSE_OPERATOR_EJECT, CAUSE_START_NEXT)))
            HourRefillPolicy.objects.filter(pk=1).update(capture_start_next=True)
            self.assertTrue(inst.install_permitted(cause_types=(CAUSE_OPERATOR_EJECT, CAUSE_START_NEXT)))
            self.assertFalse(inst.install_permitted(cause_types=("operator_skip",)))
            with transaction.atomic():
                self.assertTrue(inst.install_permitted(cause_types=(CAUSE_START_NEXT,), lock=True))
        self.assertFalse(inst.install_permitted(cause_types=(CAUSE_START_NEXT,)))   # not armed
        HourRefillPolicy.objects.filter(pk=1).update(dry_run_only=True)
        with ARMED:
            self.assertFalse(inst.install_permitted(cause_types=(CAUSE_START_NEXT,)))


# ---------------------------------------------------------------------------
# 7. Guarded installer with a start_next cause
# ---------------------------------------------------------------------------

class StartNextInstallTests(InstallFixtures, TestCase):
    def setUp(self):
        self.build()

    def start_next_cause(self, row, *, minute=30):
        from library.services.log_builder import effective_airtime_seconds

        env = StartNextCauseEnvelope(
            source_key=start_next_source_key("eng-b", "A", row.id, row.id, f"req-{row.id}"),
            engine_instance="eng-b", request_id=f"req-{row.id}", slot="A", deck_generation=row.id,
            log_item_id=row.id, cut_position_seconds=120.0, remaining_effective_airtime_seconds=120.0,
        )
        log = row.playlist_log
        evidence = StartNextCauseEvidence(
            envelope=env, occurred_at=refill_at(14, minute), playlist_log_id=log.id, log_date=log.date,
            log_hour=log.hour, log_item_position=row.position, scheduled_category_id=row.category_id,
            scheduled_category_code=row.category.code, scheduled_category_kind_code="music",
            original_track_id=row.track_id, original_track_title=row.track_title, original_track_artist="",
            original_effective_airtime_seconds=effective_airtime_seconds(row.track),
        )
        return HourRefillCause.objects.create(**evidence.cause_fields())

    def cut_engine(self, future=(290.0, 290.0)):
        """Phase B's hour (net 389.25 s) with the finished item 1 cut by Start Next."""
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        cut_row = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        for i, seconds in enumerate(future):
            self.row(self.track(seconds), 3 + i)
        self.start_next_cause(cut_row)
        return self.engine(cursor=3, deck_index=2, deck_position=10.0)

    def test_a_start_next_cause_installs_once_with_its_category(self):
        engine = self.cut_engine()
        self.track(200.0), self.track(180.0), self.track(170.0)
        result = self.attempt(engine)
        self.assertEqual(result.status, inst.STATUS_INSTALLED, result.reasons)
        ledger = HourRefillInstall.objects.get()
        self.assertEqual(ledger.cause_ids, [HourRefillCause.objects.get().id])
        self.assertEqual(set(LogItem.objects.filter(id__in=result.installed_log_item_ids)
                             .values_list("category_id", flat=True)), {self.gold.id})
        self.assertEqual(self.attempt(engine).status, inst.STATUS_ADOPTION_PENDING)
        self.reload(engine)
        self.assertNotEqual(self.attempt(engine).status, inst.STATUS_INSTALLED)
        self.assertEqual(HourRefillInstall.objects.count(), 1)

    def test_the_real_policy_gate_refuses_while_the_start_next_switch_is_off(self):
        engine = self.cut_engine()
        self.track(200.0), self.track(180.0)
        HourRefillPolicy(enabled=True, capture_operator_eject=True, capture_request_substitution=True,
                         dry_run_only=False).save()
        before = self.rows()
        with ARMED:
            refused = self.installer(engine).attempt()
        self.assertEqual((refused.status, refused.reasons),
                         (inst.STATUS_POLICY_REFUSED, ("not_permitted_for_cause_types",)))
        self.assertEqual(self.rows(), before)
        HourRefillPolicy.objects.filter(pk=1).update(capture_start_next=True)
        with ARMED:
            installed = self.installer(engine).attempt()
        self.assertEqual(installed.status, inst.STATUS_INSTALLED, installed.reasons)

    def test_default_dry_run_and_unarmed_host_never_write(self):
        engine = self.cut_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        HourRefillPolicy(enabled=True, capture_start_next=True).save()        # dry_run_only default True
        with ARMED:
            self.assertEqual(self.installer(engine).attempt().status, inst.STATUS_DRY_RUN_ONLY)
        HourRefillPolicy.objects.filter(pk=1).update(dry_run_only=False)
        self.assertEqual(self.installer(engine).attempt().status, inst.STATUS_DRY_RUN_ONLY)   # not armed
        self.assertEqual(self.rows(), before)
        self.assertFalse(HourRefillInstall.objects.exists())

    def test_no_install_during_an_active_transition_or_without_a_live_deck(self):
        for label, change in [
            ("start next active", lambda e: setattr(e, "_start_next_active", {"request_id": "r"})),
            ("transition pending", lambda e: setattr(e, "_next_triggered", True)),
            ("manual", lambda e: setattr(e, "manual_mode", True)),
            ("no live deck", lambda e: e.decks.__setitem__("A", None)),
        ]:
            with self.subTest(label):
                HourRefillCause.objects.all().delete()
                LogItem.objects.all().delete()
                PlaylistLog.objects.exclude(pk=self.log.pk).delete()
                engine = self.cut_engine()
                self.track(200.0), self.track(180.0)
                change(engine)
                before = self.rows()
                result = self.attempt(engine)
                self.assertNotEqual(result.status, inst.STATUS_INSTALLED)
                self.assertEqual(self.rows(), before)
        self.assertFalse(HourRefillInstall.objects.exists())

    def test_a_cut_still_on_the_deck_is_not_counted(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        on_deck = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(290.0), 2)
        self.start_next_cause(on_deck)
        engine = self.engine(cursor=2, deck_index=1, deck_position=10.0)
        self.track(200.0), self.track(180.0)
        result = self.attempt(engine)
        self.assertNotEqual(result.status, inst.STATUS_INSTALLED)
        self.assertIn("no_current_hour_cause", result.reasons)
