"""Roadmap 2.25 B1 -- pure planner and eligibility (L1). No DB, no GStreamer."""

from __future__ import annotations

import ast
import inspect
import math

from django.test import SimpleTestCase

from library.services import vt_transition as vt
from library.services.vt_transition import (
    INTRO, OUTRO, IntroFacts, InvalidTransitionInput, ItemRole, LiveContext, Markers, OutroFacts,
    Reason, Recording, SideEligibility, StartKind, TransitionInput, duck_points, fire_deadlines,
    intro_eligibility, outro_eligibility, outro_resume_staleness, plan_transition, underlap,
    usable_intro_window,
)

M = vt.VOCAL_SAFETY_MARGIN_S
US = 1e-6      # every timing example is asserted to the microsecond

# The KOGR 2026-11-16 incident (permanent fixture; see the Phase A module).
OUT_18911 = Markers(duration=261.530612, cue_in=0.274376, next_start=255.943311, outro_starts=None,
                    intro_until=None)
IN_37652 = Markers(duration=209.798186, cue_in=0.124717, next_start=200.745, outro_starts=182.145,
                   intro_until=15.008)
VT_6 = Recording(attached=True, playable=True, duration=6.456229, source="production_media")
AUTO = LiveContext()


def intro_side(v):
    return SideEligibility(INTRO, True, "", v)


NONE_OUT = SideEligibility(OUTRO, False, Reason.NO_RECORDING)
NONE_IN = SideEligibility(INTRO, False, Reason.NO_RECORDING)


class ModuleIsPureTests(SimpleTestCase):
    def test_imports_nothing_from_django_gstreamer_glib_or_the_engine(self):
        tree = ast.parse(inspect.getsource(vt))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= {"__future__", "math", "dataclasses", "typing"}, imported)

    def test_no_clock_or_engine_names_in_code(self):
        tree = ast.parse(inspect.getsource(vt))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for forbidden in ("time", "datetime", "timezone", "Gst", "GLib", "objects", "engine"):
            self.assertNotIn(forbidden, names | attrs)


class IncidentTimingTests(SimpleTestCase):
    """The approved examples, to the microsecond."""

    def test_incident_intro_vt_is_eligible_without_any_outgoing_marker(self):
        outro = outro_eligibility(OutroFacts(recording=Recording(), markers=OUT_18911), AUTO)
        intro = intro_eligibility(IntroFacts(recording=VT_6, markers=IN_37652), AUTO)
        self.assertFalse(outro.eligible)
        self.assertEqual(outro.reason, Reason.NO_RECORDING)
        self.assertTrue(intro.eligible)     # F1: independent of the outgoing's Outro Starts

    def test_example_a_incident_short_vt(self):
        self.assertAlmostEqual(usable_intro_window(IN_37652), 14.883283, delta=US)
        self.assertEqual(underlap(6.456229, IN_37652), 0.0)
        a_seg = 1000.0
        plan = plan_transition(TransitionInput(segue_at=a_seg, outro=NONE_OUT, intro=intro_side(6.456229),
                                               incoming_markers=IN_37652))
        self.assertEqual(plan.intro_start, a_seg)
        self.assertEqual(plan.music_at, a_seg)
        self.assertAlmostEqual(plan.intro_end, a_seg + 6.456229, delta=US)
        self.assertAlmostEqual(plan.vocal_at, a_seg + 14.883283, delta=US)
        self.assertAlmostEqual(plan.clearance, 8.427054, delta=US)
        self.assertAlmostEqual(plan.deck_offset, a_seg - 0.124717, delta=US)
        self.assertIsNone(plan.hold_until)

    def test_example_b_long_vt_underlap(self):
        self.assertAlmostEqual(underlap(18.0, IN_37652), 3.266717, delta=US)
        plan = plan_transition(TransitionInput(segue_at=50.0, outro=NONE_OUT, intro=intro_side(18.0),
                                               incoming_markers=IN_37652))
        self.assertEqual(plan.intro_start, 50.0)
        self.assertAlmostEqual(plan.music_at, 53.266717, delta=US)
        self.assertAlmostEqual(plan.intro_end, 68.0, delta=US)
        self.assertAlmostEqual(plan.vocal_at, 68.15, delta=US)
        self.assertAlmostEqual(plan.clearance, M, delta=US)

    def test_example_c_exact_fit(self):
        markers = Markers(duration=10.0, cue_in=0.25, intro_until=2.0)
        v = 2.0 - 0.25 - M
        self.assertEqual(underlap(v, markers), 0.0)
        plan = plan_transition(TransitionInput(segue_at=0.0, outro=NONE_OUT, intro=intro_side(v),
                                               incoming_markers=markers))
        self.assertAlmostEqual(plan.clearance, M, delta=US)
        # one microsecond longer needs one microsecond of underlap
        self.assertAlmostEqual(underlap(v + 1e-6, markers), 1e-6, delta=1e-9)

    def test_example_d_missing_or_unusable_intro_until_plays_whole_vt_then_margin(self):
        for intro_until in (None, 0.0, 0.1, 0.25, 11.0, math.nan, -1.0):
            with self.subTest(intro_until=intro_until):
                markers = Markers(duration=10.0, cue_in=0.25, intro_until=intro_until)
                self.assertIsNone(usable_intro_window(markers))
                self.assertAlmostEqual(underlap(1.0, markers), 1.0 + M, delta=US)
                plan = plan_transition(TransitionInput(segue_at=5.0, outro=NONE_OUT, intro=intro_side(1.0),
                                                       incoming_markers=markers))
                self.assertAlmostEqual(plan.music_at - plan.intro_end, M, delta=US)
                self.assertIsNone(plan.vocal_at)

    def test_example_e_outro_holds_segue_then_intro_after_gap(self):
        outro = SideEligibility(OUTRO, True, "", 9.0)
        plan = plan_transition(TransitionInput(segue_at=255.943311, outro=outro, outro_at=250.0,
                                               intro=intro_side(6.456229), incoming_markers=IN_37652,
                                               gap_s=0.2))
        self.assertEqual(plan.outro_end, 259.0)
        self.assertEqual(plan.hold_until, 259.0)
        self.assertAlmostEqual(plan.intro_start, 259.2, delta=US)
        self.assertAlmostEqual(plan.music_at, 259.2, delta=US)

    def test_example_f_outro_only_outlasting_the_segue_holds_music_until_speech_ends(self):
        outro = SideEligibility(OUTRO, True, "", 15.0)
        plan = plan_transition(TransitionInput(segue_at=255.943311, outro=outro, outro_at=250.0,
                                               intro=NONE_IN, incoming_markers=IN_37652))
        self.assertEqual(plan.music_at, 265.0)
        self.assertEqual(plan.hold_until, 265.0)

    def test_outro_ending_before_segue_leaves_segue_unchanged(self):
        outro = SideEligibility(OUTRO, True, "", 4.0)
        plan = plan_transition(TransitionInput(segue_at=255.0, outro=outro, outro_at=250.0, intro=NONE_IN,
                                               incoming_markers=IN_37652))
        self.assertEqual(plan.music_at, 255.0)
        self.assertIsNone(plan.hold_until)

    def test_property_speech_always_clears_the_vocal_by_m(self):
        for cue in (0.0, 0.124717, 1.2):
            for intro_until in (cue + 0.5, cue + 3.0, cue + 15.0):
                for v in (0.2, 0.5, 2.9, 3.0, 6.456229, 18.0, 40.0):
                    markers = Markers(duration=200.0, cue_in=cue, intro_until=intro_until)
                    plan = plan_transition(TransitionInput(segue_at=10.0, outro=NONE_OUT,
                                                           intro=intro_side(v), incoming_markers=markers))
                    self.assertGreaterEqual(plan.music_at, plan.segue_at)
                    self.assertGreaterEqual(plan.clearance, M - 1e-9)
                    self.assertGreaterEqual(plan.underlap, 0.0)
                    self.assertAlmostEqual(plan.deck_offset, plan.music_at - cue, delta=US)

    def test_two_recorded_voices_never_overlap(self):
        for gap in (0.0, 0.2, 1.0):
            outro = SideEligibility(OUTRO, True, "", 5.0)
            plan = plan_transition(TransitionInput(segue_at=100.0, outro=outro, outro_at=97.0,
                                                   intro=intro_side(2.0), incoming_markers=IN_37652, gap_s=gap))
            self.assertAlmostEqual(plan.intro_start - plan.outro_end, gap, delta=US)


class DuckTests(SimpleTestCase):
    def test_speech_owned_attack_and_release(self):
        plan = plan_transition(TransitionInput(segue_at=10.0, outro=NONE_OUT, intro=intro_side(2.0),
                                               incoming_markers=IN_37652, duck_db=-6.0, ramp_s=0.3))
        g = 10 ** (-6 / 20)
        self.assertEqual(len(plan.duck_points), 4)
        times = [t for t, _ in plan.duck_points]
        self.assertEqual(times, sorted(times))
        self.assertAlmostEqual(plan.duck_points[0][0], 9.7, delta=US)       # attack before speech
        self.assertAlmostEqual(plan.duck_points[1][1], g, delta=US)
        self.assertAlmostEqual(plan.duck_points[2][0], 12.0, delta=US)      # release at speech end
        self.assertAlmostEqual(plan.duck_points[3][0], 12.3, delta=US)

    def test_close_intervals_merge_far_ones_do_not(self):
        g = 0.5
        self.assertEqual(len(duck_points([(0.0, 1.0), (1.2, 2.0)], 0.1, g)), 4)
        self.assertEqual(len(duck_points([(0.0, 1.0), (1.3, 2.0)], 0.1, g)), 8)

    def test_no_speech_or_no_attenuation_means_no_duck(self):
        self.assertEqual(duck_points([], 0.3, 0.5), ())
        plan = plan_transition(TransitionInput(segue_at=10.0, outro=NONE_OUT, intro=intro_side(2.0),
                                               incoming_markers=IN_37652, duck_db=0.0))
        self.assertEqual(plan.duck_points, ())
        plan = plan_transition(TransitionInput(segue_at=10.0, outro=NONE_OUT, intro=NONE_IN,
                                               incoming_markers=IN_37652))
        self.assertEqual(plan.duck_points, ())
        self.assertFalse(plan.has_speech)


class OutroEligibilityTests(SimpleTestCase):
    def facts(self, **overrides):
        values = dict(recording=VT_6, markers=Markers(duration=200.0, cue_in=0.5, next_start=195.0,
                                                       outro_starts=180.0))
        values.update(overrides)
        return OutroFacts(**values)

    def test_eligible(self):
        self.assertEqual(outro_eligibility(self.facts(), AUTO), SideEligibility(OUTRO, True, "", 6.456229))

    def test_record_is_not_eligibility(self):
        cases = {
            Reason.NO_RECORDING: Recording(),
            Reason.RECORDING_UNPLAYABLE: Recording(attached=True, playable=False, duration=5.0),
            Reason.INVALID_VT_DURATION: Recording(attached=True, playable=True, duration=0.0),
        }
        for reason, recording in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(outro_eligibility(self.facts(recording=recording), AUTO).reason, reason)
        bad = [None, math.nan, math.inf, -1.0]
        for duration in bad:
            r = Recording(attached=True, playable=True, duration=duration)
            self.assertEqual(outro_eligibility(self.facts(recording=r), AUTO).reason, Reason.INVALID_VT_DURATION)

    def test_markers(self):
        base = dict(duration=200.0, cue_in=0.5, next_start=195.0)
        cases = [
            (dict(outro_starts=None), Reason.OUTRO_MARKER_MISSING),          # D4 (the incident's outgoing)
            (dict(outro_starts=0.0), Reason.OUTRO_MARKER_INVALID),
            (dict(outro_starts=0.5), Reason.OUTRO_MARKER_INVALID),           # at Cue In
            (dict(outro_starts=math.nan), Reason.OUTRO_MARKER_INVALID),
            (dict(outro_starts=195.0), Reason.OUTRO_MARKER_AT_OR_AFTER_SEGUE),   # D7 boundary
            (dict(outro_starts=196.0), Reason.OUTRO_MARKER_AT_OR_AFTER_SEGUE),
            (dict(outro_starts=150.0, next_start=250.0), Reason.OUTRO_MARKER_INVALID),  # next_start > duration
            (dict(outro_starts=150.0, duration=0.0), Reason.INVALID_TRACK_DURATION),
            (dict(outro_starts=150.0, cue_in=-1.0), Reason.INVALID_CUE_IN),
        ]
        for change, reason in cases:
            with self.subTest(change=change):
                markers = Markers(**{**base, **change})
                self.assertEqual(outro_eligibility(self.facts(markers=markers), AUTO).reason, reason)
        just_before = Markers(**{**base, "outro_starts": 194.999999})
        self.assertTrue(outro_eligibility(self.facts(markers=just_before), AUTO).eligible)
        no_next_start = Markers(duration=200.0, cue_in=0.0, outro_starts=199.0)
        self.assertTrue(outro_eligibility(self.facts(markers=no_next_start), AUTO).eligible)

    def test_live_talent_and_manual(self):
        self.assertEqual(outro_eligibility(self.facts(), LiveContext(auto_mode=False)).reason, Reason.NOT_AUTO)
        self.assertEqual(outro_eligibility(self.facts(), LiveContext(mic_live=True)).reason, Reason.MIC_LIVE)
        self.assertEqual(outro_eligibility(self.facts(), LiveContext(remote_dj_live=True)).reason,
                         Reason.REMOTE_DJ_LIVE)

    def test_d13_resume_and_replay(self):
        self.assertEqual(outro_eligibility(self.facts(already_aired=True), AUTO).reason,
                         Reason.OUTRO_ALREADY_AIRED)
        self.assertEqual(outro_eligibility(self.facts(generation_start_position=180.0), AUTO).reason,
                         Reason.GENERATION_STARTED_PAST_MARKER)
        # Continuation started early with fresh evidence: delivery cannot have happened.
        early = self.facts(is_continuation=True, generation_start_position=30.0,
                           resume_staleness_s=outro_resume_staleness(4.0))
        self.assertTrue(outro_eligibility(early, AUTO).eligible)
        # Near the marker, or unknown evidence age: uncertain -> suppressed.
        near = self.facts(is_continuation=True, generation_start_position=175.0,
                          resume_staleness_s=outro_resume_staleness(4.0))
        self.assertEqual(outro_eligibility(near, AUTO).reason, Reason.OUTRO_DELIVERY_UNCERTAIN)
        unknown = self.facts(is_continuation=True, generation_start_position=1.0,
                             resume_staleness_s=outro_resume_staleness(None))
        self.assertEqual(outro_eligibility(unknown, AUTO).reason, Reason.OUTRO_DELIVERY_UNCERTAIN)

    def test_nonsensical_generation_facts_raise(self):
        for start in (math.nan, -1.0):
            with self.assertRaises(InvalidTransitionInput):
                outro_eligibility(self.facts(generation_start_position=start), AUTO)
        with self.assertRaises(InvalidTransitionInput):
            outro_eligibility(self.facts(resume_staleness_s=-1.0), AUTO)


class IntroEligibilityTests(SimpleTestCase):
    def facts(self, **overrides):
        values = dict(recording=VT_6, markers=IN_37652)
        values.update(overrides)
        return IntroFacts(**values)

    def test_automatic_start_is_eligible_including_promos_and_ids(self):
        self.assertTrue(intro_eligibility(self.facts(), AUTO).eligible)
        self.assertTrue(intro_eligibility(self.facts(role=ItemRole.ORDINARY), AUTO).eligible)

    def test_d5_operator_starts_and_handoff_bypass(self):
        for kind in (StartKind.START_NEXT, StartKind.MANUAL, StartKind.EJECT_REPLACEMENT):
            with self.subTest(kind=kind):
                self.assertEqual(intro_eligibility(self.facts(start_kind=kind), AUTO).reason,
                                 Reason.OPERATOR_START)
        self.assertEqual(intro_eligibility(self.facts(start_kind=StartKind.MANUAL_TO_AUTO_HANDOFF), AUTO).reason,
                         Reason.MANUAL_TO_AUTO_HANDOFF)

    def test_d13_recovery_never_replays_an_intro(self):
        self.assertEqual(intro_eligibility(self.facts(start_kind=StartKind.RESUME), AUTO).reason,
                         Reason.INTERRUPTED_OCCURRENCE_RECOVERY)
        self.assertEqual(intro_eligibility(self.facts(start_kind=StartKind.CONTINUATION), AUTO).reason,
                         Reason.CONTINUATION_GENERATION)
        self.assertEqual(intro_eligibility(self.facts(already_claimed=True), AUTO).reason,
                         Reason.ALREADY_CLAIMED)

    def test_d14_protected_paths(self):
        cases = {
            ItemRole.URGENT: Reason.PROTECTED_URGENT,
            ItemRole.DEDICATION_INTRO: Reason.PROTECTED_DEDICATION,
            ItemRole.DEDICATION_FOLLOWUP: Reason.PROTECTED_DEDICATION,
            ItemRole.ACTIVE_MESSAGE: Reason.PROTECTED_ACTIVE_MESSAGE,
        }
        for role, reason in cases.items():
            with self.subTest(role=role):
                self.assertEqual(intro_eligibility(self.facts(role=role), AUTO).reason, reason)

    def test_recordings_and_markers(self):
        self.assertEqual(intro_eligibility(self.facts(recording=Recording()), AUTO).reason, Reason.NO_RECORDING)
        self.assertEqual(intro_eligibility(self.facts(recording=Recording(attached=True)), AUTO).reason,
                         Reason.RECORDING_UNPLAYABLE)
        self.assertTrue(intro_eligibility(
            self.facts(recording=Recording(attached=True, playable=True, duration=3.0, source="legacy")), AUTO
        ).eligible)
        for markers, reason in (
            (Markers(duration=None), Reason.INVALID_TRACK_DURATION),
            (Markers(duration=-5.0), Reason.INVALID_TRACK_DURATION),
            (Markers(duration=10.0, cue_in=10.0), Reason.INVALID_CUE_IN),
            (Markers(duration=10.0, cue_in=math.inf), Reason.INVALID_CUE_IN),
        ):
            with self.subTest(markers=markers):
                self.assertEqual(intro_eligibility(self.facts(markers=markers), AUTO).reason, reason)
        # D10: missing/zero/invalid Intro Until does NOT suppress the intro VT.
        for intro_until in (None, 0.0, -2.0, 300.0):
            markers = Markers(duration=200.0, cue_in=0.1, intro_until=intro_until)
            self.assertTrue(intro_eligibility(self.facts(markers=markers), AUTO).eligible)

    def test_live_talent(self):
        self.assertEqual(intro_eligibility(self.facts(), LiveContext(mic_live=True)).reason, Reason.MIC_LIVE)
        self.assertEqual(intro_eligibility(self.facts(), LiveContext(auto_mode=False)).reason, Reason.NOT_AUTO)

    def test_unknown_kinds_raise(self):
        with self.assertRaises(InvalidTransitionInput):
            intro_eligibility(self.facts(start_kind="bogus"), AUTO)
        with self.assertRaises(InvalidTransitionInput):
            intro_eligibility(self.facts(role="bogus"), AUTO)

    def test_sides_are_independent(self):
        outro = outro_eligibility(OutroFacts(recording=VT_6, markers=Markers(duration=200.0, outro_starts=None)),
                                  AUTO)
        intro = intro_eligibility(self.facts(), AUTO)
        self.assertFalse(outro.eligible)
        self.assertTrue(intro.eligible)
        outro2 = outro_eligibility(OutroFacts(recording=VT_6, markers=Markers(duration=200.0, outro_starts=150.0)),
                                   AUTO)
        intro2 = intro_eligibility(self.facts(start_kind=StartKind.START_NEXT), AUTO)
        self.assertTrue(outro2.eligible)
        self.assertFalse(intro2.eligible)


class UrgentRuleTests(SimpleTestCase):
    def test_urgent_next_suppresses_an_outro_that_would_hold(self):
        outro = SideEligibility(OUTRO, True, "", 9.0)
        plan = plan_transition(TransitionInput(segue_at=255.0, outro=outro, outro_at=250.0, intro=NONE_IN,
                                               incoming_markers=IN_37652, next_is_urgent=True))
        self.assertIsNone(plan.outro_start)
        self.assertEqual(plan.outro_reason, Reason.URGENT_NEXT_NO_HOLD)
        self.assertEqual(plan.music_at, 255.0)
        self.assertIsNone(plan.hold_until)

    def test_urgent_next_keeps_an_outro_that_ends_by_the_segue(self):
        outro = SideEligibility(OUTRO, True, "", 5.0)
        plan = plan_transition(TransitionInput(segue_at=255.0, outro=outro, outro_at=250.0, intro=NONE_IN,
                                               incoming_markers=IN_37652, next_is_urgent=True))
        self.assertEqual(plan.outro_end, 255.0)
        self.assertEqual(plan.music_at, 255.0)


class InvalidInputTests(SimpleTestCase):
    def test_non_physical_inputs_raise(self):
        base = dict(segue_at=1.0, outro=NONE_OUT, intro=intro_side(1.0), incoming_markers=IN_37652)
        for change in (dict(segue_at=math.nan), dict(segue_at=math.inf), dict(gap_s=-0.1), dict(ramp_s=-1.0),
                       dict(duck_db=3.0), dict(duck_db=math.nan), dict(margin_s=-0.1),
                       dict(incoming_markers=Markers(duration=None)),
                       dict(outro=SideEligibility(OUTRO, True, "", 2.0), outro_at=None)):
            with self.subTest(change=change):
                with self.assertRaises(InvalidTransitionInput):
                    plan_transition(TransitionInput(**{**base, **change}))
        for v in (0.0, -1.0, math.nan):
            with self.assertRaises(InvalidTransitionInput):
                underlap(v, IN_37652)
        with self.assertRaises(InvalidTransitionInput):
            duck_points([(2.0, 1.0)], 0.1, 0.5)

    def test_fire_deadlines_are_measured_from_admission_not_preparation(self):
        d = fire_deadlines(100.0, 6.0)
        self.assertAlmostEqual(d["ready_by"], 100.0 - vt.READY_LEAD_S, delta=US)
        self.assertAlmostEqual(d["first_audio_by"], 101.0, delta=US)
        self.assertAlmostEqual(d["complete_by"], 108.0, delta=US)
        with self.assertRaises(InvalidTransitionInput):
            fire_deadlines(100.0, 0.0)


class NegativeControlTests(SimpleTestCase):
    """Representative mutations of the model that these tests must catch."""

    def test_back_timing_from_the_vocal_would_be_detected(self):
        # r0144-style back-timing (VT ends exactly at the vocal, start = I - V) is NOT the approved D1.
        plan = plan_transition(TransitionInput(segue_at=0.0, outro=NONE_OUT, intro=intro_side(6.456229),
                                               incoming_markers=IN_37652))
        back_timed_start = plan.vocal_at - 6.456229
        self.assertNotAlmostEqual(plan.intro_start, back_timed_start, delta=0.001)

    def test_margin_is_required(self):
        markers = Markers(duration=10.0, cue_in=0.0, intro_until=2.0)
        self.assertGreater(underlap(2.0, markers), underlap(2.0, markers, margin=0.0))
