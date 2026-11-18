"""Roadmap 2.25 Phase A -- reproductions of the r0144 evergreen Voice Track
transition defects, on the real transport topology (library/tests/
_transport_harness.py: real decodebin decks, silence prime, audiomixer request
pads, the real VT state machine and GLib timers, real VT fires through the FX
submix).

Phase A characterized the r0144 defects (each test asserted the defect and
passed on r0144). Roadmap 2.25 B3/B4 convert them into PERMANENT correctness
assertions where B3/B4 implement the behaviour; the r0144 defect assertions
remain in git history (6ad5222) as the negative-control record.

Every characterization is converted: B3 (outgoing side) R4a, R6, R9, R11;
B4 (incoming side) R1a, R1b, R2, R3, R3b, R4b, R5, R7, R8, R10b. R0 (the
ordinary segue) and R10 (the legacy fire's latency) remain measurements.

Signals (whole-bin Goertzel at multiples of 100 Hz, 10 ms program buffers):
400 Hz outgoing music, 500 Hz incoming music, 300 Hz urgent insert, 600 Hz
request replacement, 700 Hz intro VT, 900 Hz outro VT, 1200 Hz VT head marker.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import time
import wave
from types import SimpleNamespace
from unittest.mock import patch

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib  # noqa: E402

from django.test import SimpleTestCase  # noqa: E402

import library.services.engine as eng_module  # noqa: E402
from library.services import vt_transition  # noqa: E402
from library.tests._transport_harness import (  # noqa: E402
    RATE,
    TransportHarness,
    _VTQuery,
    fake_voicetrack,
    make_item,
    make_track,
    pump,
    running_seconds,
    wait_until,
)

# ---------------------------------------------------------------------------
# Permanent synthetic regression fixture: KOGR, 2026-11-16 20:45:04.
# Metadata only -- no station music or recording is used or required.
# ---------------------------------------------------------------------------
INCIDENT_2026_11_16 = {
    "outgoing": {
        "track_id": 18911, "title": "Rain Down Tears", "artist": "Phantom Blues Band",
        "duration": 261.530612, "cue_in": 0.274376, "next_start": 255.943311,
        "outro_starts": None, "intro_until": None, "voicetracks": {},
    },
    "incoming": {
        "track_id": 37652, "title": "'99 Mustang (promo)", "artist": "Caleb Nelsen",
        "duration": 209.798186, "cue_in": 0.124717, "next_start": 200.745,
        "outro_starts": 182.145, "intro_until": 15.008,
        "voicetracks": {"intro": {
            "voicetrack_id": 6, "gain_db": 0.0, "duration": 6.456229,
            "media": "4b39b81c-3679-4260-ae0f-bbcea33136ff",
        }},
    },
    # Trigger = next_start(out) - cue_in(in) = 255.943311 - 0.124717 = 255.818594
    "observed_trigger_position": 255.8,
}

OUT_HZ, IN_HZ, URGENT_HZ, REQUEST_HZ, INTRO_HZ, OUTRO_HZ, MARKER_HZ = 400, 500, 300, 600, 700, 900, 1200
PRESENT = 0.05   # Goertzel amplitude; a 0.4-amplitude tone reads ~0.4, a -6 dB duck ~0.2
POLL_STUBS = ("_check_commands", "_check_stuck_decks", "_reload_queue_if_changed",
              "_sweep_active_message_queue", "_warm_track_cache")


def write_segments(path, segments, amplitude=0.4):
    """Stereo 16-bit WAV of consecutive (seconds, freq) segments; freq 0 = silence."""
    peak = int(32767 * amplitude)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(RATE)
        frames = []
        for seconds, freq in segments:
            for index in range(int(RATE * seconds)):
                value = 0 if freq <= 0 else int(peak * math.sin(2 * math.pi * freq * index / RATE))
                frames.append(struct.pack("<hh", value, value))
        output.writeframes(b"".join(frames))
    return path


def patch_vt_lookup(testcase, table, *, duck_db=-6.0, ramp_ms=40, gap_ms=200):
    """VoiceTrack lookups keyed by (track_id, position); records every lookup."""
    lookups = []

    def vt_filter(**kwargs):
        key = (getattr(kwargs.get("track"), "id", None), kwargs.get("position"))
        lookups.append(key)
        return _VTQuery(table.get(key))

    config = SimpleNamespace(program_duck_db=duck_db, duck_ramp_ms=ramp_ms, min_gap_ms=gap_ms)
    for mocked in (
        patch.object(eng_module.VoiceTrack.objects, "filter", new=vt_filter),
        patch.object(eng_module.VoiceTrackConfig, "load", new=lambda: config),
        patch.object(eng_module.SongRequest.objects, "filter",
                     new=lambda **_kwargs: SimpleNamespace(exists=lambda: False)),
        patch.object(eng_module, "_close_old_connections", new=lambda: None),
    ):
        mocked.start()
        testcase.addCleanup(mocked.stop)
    return lookups


def spy_fires(harness):
    """Record every VT fire: kind, file, fire id, running time, decks live at that moment."""
    fires = []
    original = harness.engine._vt_fire_file

    def spy(filepath, gain_db, kind):
        fire_id = original(filepath, gain_db, kind)
        fires.append({"kind": kind, "path": str(filepath), "fire_id": fire_id,
                      "running": running_seconds(harness.engine), "live": harness.live_ids(),
                      "manual_mode": harness.engine.manual_mode, "mic_live": harness.engine.mic_live})
        return fire_id

    harness.engine._vt_fire_file = spy
    return fires


def start_poll_loop(testcase, engine):
    """The production 250 ms _poll_position tick, minus the DB/IPC side work."""
    for name in POLL_STUBS:
        setattr(engine, name, lambda *_args, **_kwargs: None)
    source = GLib.timeout_add(eng_module.POSITION_POLL_MS, engine._poll_position)

    def _remove():
        if GLib.MainContext.default().find_source_by_id(source):
            GLib.source_remove(source)
    testcase.addCleanup(_remove)


def poll_at(engine, deck, position):
    deck.started_at = time.time() - 600.0
    with patch.object(type(engine), "_get_deck_position", lambda _self, _deck: position):
        engine._poll_position()


def set_markers(item, *, duration=None, cue_in=None, next_start=None, outro_starts="keep",
                intro_until=None):
    track = item.track
    if duration is not None:
        track.duration_seconds = duration
    if cue_in is not None:
        track.cue_in_seconds = cue_in
    if next_start is not None:
        track.next_start_seconds = next_start
    if outro_starts != "keep":
        track.outro_starts_seconds = outro_starts
    if intro_until is not None:
        track.intro_until_seconds = intro_until


def onset(harness, freq, after=0.0, threshold=PRESENT, sustain=3):
    """First running time from which ``freq`` is present for ``sustain``
    consecutive 10 ms buffers (a tone starting mid-buffer leaks into
    neighbouring bins for one buffer; that is not an onset)."""
    series = [(running, level) for running, level in harness.tone_series(freq) if running >= after]
    for index in range(len(series) - sustain + 1):
        if all(level >= threshold for _running, level in series[index:index + sustain]):
            return series[index][0]
    return None


def present_seconds(harness, freq, start=0.0, end=1e9, threshold=PRESENT):
    return sum(0.01 for running, level in harness.tone_series(freq)
               if start <= running < end and level >= threshold)


def report(label, **values):
    rendered = " ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in values.items())
    print(f"\nPHASE-A-METRIC {label} {rendered}")


# ---------------------------------------------------------------------------
# R1 -- the November 16 incident.
# ---------------------------------------------------------------------------
class R1IncidentExactMarkersTests(SimpleTestCase):
    """R1a: the incident's exact cue markers at the observed trigger position."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        vt = INCIDENT_2026_11_16["incoming"]["voicetracks"]["intro"]
        self.intro = fake_voicetrack(self.h.media("incident-intro-vt", seconds=1.0, freq=INTRO_HZ),
                                     vt["duration"], gain_db=vt["gain_db"])
        self.lookups = patch_vt_lookup(self, {(INCIDENT_2026_11_16["incoming"]["track_id"], "intro"): self.intro})
        self.fires = spy_fires(self.h)
        for name in POLL_STUBS:
            setattr(self.engine, name, lambda *_args, **_kwargs: None)
        out, inc = INCIDENT_2026_11_16["outgoing"], INCIDENT_2026_11_16["incoming"]
        self.items = self.h.queue({"id": 1, "track_id": out["track_id"], "seconds": 8},
                                  {"id": 2, "track_id": inc["track_id"], "seconds": 8, "freq": IN_HZ})
        for item, spec in zip(self.items, (out, inc)):
            set_markers(item, duration=spec["duration"], cue_in=spec["cue_in"],
                        next_start=spec["next_start"], outro_starts=spec["outro_starts"],
                        intro_until=spec["intro_until"] or 0.0)
        self.h.play()

    def test_incident_markers_air_the_incoming_intro_vt_without_an_outgoing_marker(self):
        # r0144 (F1): never looked up, never aired. Now the claimed incoming's
        # own intro VT is decided at its claim, whatever the outgoing carries.
        deck = self.engine._start_next_track(slot="A") or self.engine.decks["A"]
        pump(0.3)
        poll_at(self.engine, deck, INCIDENT_2026_11_16["observed_trigger_position"] + 0.05)
        self._assert_incident_intro_planned()

    def _assert_incident_intro_planned(self):
        self.assertEqual(self.h.claims, [1, 2], "the trigger started '99 Mustang")
        self.assertIn((INCIDENT_2026_11_16["incoming"]["track_id"], "intro"), self.lookups)
        record = self.engine.__dict__.get("_vtt")
        self.assertIsNotNone(record)
        self.assertTrue(record.committed)
        self.assertEqual(record.incoming["item_id"], 2)
        self.assertEqual(record.plan.underlap, 0.0)                         # contract section 4, example A
        self.assertAlmostEqual(record.plan.clearance, 8.427054, places=6)
        self.assertTrue(wait_until(lambda: record.intro_fire is not None, 2.0))
        owner = record.intro_fire.owner
        self.assertEqual((owner.direction, owner.log_item_id, owner.track_id),
                         ("intro", 2, INCIDENT_2026_11_16["incoming"]["track_id"]))
        self.assertTrue(wait_until(lambda: record.intro_fire.state in ("admitted", "draining", "ended"), 2.0))

    def test_control_an_outgoing_outro_marker_does_not_change_the_intro_result(self):
        # The former negative control (only the OUTGOING marker changed) now
        # gives the identical intro result: the two sides are independent.
        set_markers(self.items[0], outro_starts=250.0)
        deck = self.engine._start_next_track(slot="A") or self.engine.decks["A"]
        pump(0.3)
        poll_at(self.engine, deck, INCIDENT_2026_11_16["observed_trigger_position"] + 0.05)
        self._assert_incident_intro_planned()

class R1IncidentRealTimeTests(SimpleTestCase):
    """R1b: the incident shape in real time (production 250 ms poll), real audio."""

    def setUp(self):
        self.h = TransportHarness(self, appsink=True)
        self.engine = self.h.engine
        intro = fake_voicetrack(self.h.media("intro-vt", seconds=1.25, freq=INTRO_HZ), 1.25)
        self.lookups = patch_vt_lookup(self, {(1002, "intro"): intro})
        self.fires = spy_fires(self.h)
        items = self.h.queue({"id": 1, "seconds": 9.0, "freq": OUT_HZ, "next_start": 8.0},
                             {"id": 2, "seconds": 6.0, "freq": IN_HZ, "lead_silence": 0.12, "cue_in": 0.12})
        set_markers(items[0], outro_starts=None)
        set_markers(items[1], intro_until=3.0)
        start_poll_loop(self, self.engine)
        self.h.play()

    def test_the_valid_intro_vt_reaches_program_after_an_unmarked_outgoing(self):
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 14.0))
        pump(2.5)
        vt_on = onset(self.h, INTRO_HZ)
        music_on = onset(self.h, IN_HZ)
        report("R1b-B4", intro_vt_seconds_on_air=present_seconds(self.h, INTRO_HZ),
               music_minus_vt=music_on - vt_on)
        self.assertIn((1002, "intro"), self.lookups)
        self.assertGreater(present_seconds(self.h, INTRO_HZ), 1.1, "the whole intro VT aired")
        self.assertLess(abs(music_on - vt_on), 0.02, "music with the speech (U = 0, D1)")

# ---------------------------------------------------------------------------
# R2/R3 -- the incoming intro VT is bound to the LOOKAHEAD, not to the
# occurrence that actually starts.
# ---------------------------------------------------------------------------
class WrongOccurrenceTests(SimpleTestCase):
    def setUp(self):
        self.h = TransportHarness(self, appsink=True)
        self.engine = self.h.engine
        intro = fake_voicetrack(self.h.media("song2-intro-vt", seconds=1.0, freq=INTRO_HZ), 1.0)
        self.lookups = patch_vt_lookup(self, {(1002, "intro"): intro})
        self.fires = spy_fires(self.h)
        self.items = self.h.queue({"id": 1, "seconds": 9.0, "freq": OUT_HZ, "next_start": 8.0},
                                  {"id": 2, "seconds": 6.0, "freq": IN_HZ})
        set_markers(self.items[0], outro_starts=2.0)
        set_markers(self.items[1], intro_until=3.0)
        self.h.play()

    def test_r2_an_urgent_insert_is_never_talked_over_by_another_occurrences_vt(self):
        start_poll_loop(self, self.engine)
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.engine.__dict__.get("_vtt") is not None, 6.0),
                        "song 2's intro VT is predicted (timing only)")
        urgent = make_item(make_track(self.h.media("urgent", seconds=4.0, freq=URGENT_HZ), 2050,
                                      duration=4.0), 50)
        urgent.playback_claimed_at = None
        urgent.category_id = 77
        urgent.category = SimpleNamespace(code="WxAlert")
        self.engine.log_items.insert(self.engine._queue_cursor, urgent)
        self.engine._vtt_mark_urgent(urgent)
        self.engine._vtt_queue_changed("urgent_insert")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 50], 12.0))
        pump(1.5)
        alert_on = onset(self.h, URGENT_HZ)
        overlap = present_seconds(self.h, INTRO_HZ, start=(alert_on or 0) - 0.5, end=(alert_on or 0) + 1.5)
        report("R2-B4", alert_onset=alert_on, foreign_vt_seconds_over_alert=overlap)
        self.assertIsNotNone(alert_on)
        self.assertLess(overlap, 0.02, "no VT over the alert (r0144: 0.93 s)")

    def test_r3_a_request_swapped_in_at_the_claim_never_carries_the_scheduled_songs_vt(self):
        replacement = make_item(make_track(self.h.media("request", seconds=5.0, freq=REQUEST_HZ), 3003,
                                           duration=5.0), 2)
        replacement.playback_claimed_at = None
        swap = patch.object(eng_module, "maybe_schedule_song_request",
                            new=lambda item: replacement if item.id == 2 else None)
        swap.start()
        self.addCleanup(swap.stop)
        start_poll_loop(self, self.engine)
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 12.0))
        pump(1.5)
        on_air = next(d for d in self.engine.decks.values() if d is not None and d.log_item.id == 2)
        self.assertEqual(on_air.track.id, 3003, "the request replaced song 2 at start")
        request_on = onset(self.h, REQUEST_HZ)
        overlap = present_seconds(self.h, INTRO_HZ, start=(request_on or 0) - 0.5, end=(request_on or 0) + 1.5)
        report("R3-B4", request_onset=request_on, song2_vt_seconds_over_request=overlap)
        self.assertLess(overlap, 0.02, "r0144: 0.3+ s of song 2's VT over the request")

    def test_r3b_a_dedication_spliced_at_the_claim_never_meets_a_second_recorded_voice(self):
        dedication = make_item(make_track(self.h.media("dedication", seconds=3.0, freq=800), 4004,
                                          duration=3.0), 60)
        dedication.playback_claimed_at = None
        dedication.category_id = 99
        dedication.category = SimpleNamespace(code="Dedications")
        self.engine._maybe_insert_dedication_intro = lambda item: dedication if item.id == 2 else item
        self.engine._restore_followup_for_intro = lambda _item: True
        start_poll_loop(self, self.engine)
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 60], 12.0))
        pump(1.5)
        spoken = onset(self.h, 800)
        overlap = present_seconds(self.h, INTRO_HZ, start=(spoken or 0) - 0.5, end=(spoken or 0) + 1.5)
        report("R3b-B4", dedication_onset=spoken, intro_vt_seconds_over_dedication=overlap)
        self.assertIsNotNone(spoken)
        self.assertLess(overlap, 0.02, "r0144: two recorded voices (0.63 s overlap)")

# ---------------------------------------------------------------------------
# R4 -- a VT fire that never completes strands the transition.
# ---------------------------------------------------------------------------
class FailedFireTests(SimpleTestCase):
    def setUp(self):
        self.h = TransportHarness(self, appsink=True)
        self.engine = self.h.engine
        corrupt = self.h.root / "corrupt-vt.flac"
        corrupt.write_bytes(b"\x00\xffnot audio at all\x13\x37" * 4096)
        self.corrupt = fake_voicetrack(corrupt, 1.0)
        self.fires = spy_fires(self.h)

    def test_r4a_undecodable_outro_vt_fails_bounded_without_dead_air(self):
        # r0144: outro_tail forever, dead air, program left ducked (F3).
        patch_vt_lookup(self, {(1001, "outro"): self.corrupt})
        items = self.h.queue({"id": 1, "seconds": 2.5, "freq": OUT_HZ},
                             {"id": 2, "seconds": 6.0, "freq": IN_HZ})
        set_markers(items[0], outro_starts=0.5)
        self.h.play()
        deck = self.engine._start_next_track(slot="A") or self.engine.decks["A"]
        pump(0.05)
        self.engine._vtt_poll(deck)                 # the production poll's planning step
        record = self.engine.__dict__.get("_vtt")
        self.assertIsNotNone(record)
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 6.0), "dead air: nothing started")
        pump(1.5)
        ended = max(run for run, level in self.h.tone_series(OUT_HZ) if level >= PRESENT) + 0.01
        self.assertEqual(record.outro_fire.state, "failed")
        self.assertTrue(record.done)
        self.assertEqual(self.engine._vt["phase"], "idle")
        curve = self.engine.__dict__.get("_vt2_duck")
        self.assertAlmostEqual(1.0 if curve is None else curve.value_at(self.engine._vt2_running_ns()), 1.0,
                               places=3)
        incoming_on = onset(self.h, IN_HZ, after=ended - 0.5)
        self.assertLess(incoming_on - ended, 0.6, "the next song follows the outgoing (ordinary EOS start)")
        report("R4a-B3", fire_reason=record.outro_fire.reason, incoming_after_outgoing_end=incoming_on - ended)

    def test_r4b_an_undecodable_intro_vt_fails_bounded_and_never_leaves_the_program_ducked(self):
        patch_vt_lookup(self, {(1002, "intro"): self.corrupt})
        items = self.h.queue({"id": 1, "seconds": 2.5, "freq": OUT_HZ},
                             {"id": 2, "seconds": 8.0, "freq": IN_HZ})
        set_markers(items[1], intro_until=3.0)
        start_poll_loop(self, self.engine)
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 6.0))
        pump(3.0)
        start_on = onset(self.h, IN_HZ)
        levels = [lvl for run, lvl in self.h.tone_series(IN_HZ) if start_on and run > start_on + 0.5]
        report("R4b-B4", incoming_peak_level=max(levels), phase=self.engine._vt["phase"])
        self.assertEqual(self.engine._vt["phase"], "idle", "r0144: intro_playing forever")
        self.assertGreater(min(levels), 0.35, "incoming music at unity (r0144: ducked for good)")
        self.assertIsNone(self.engine.__dict__.get("_vtt"), "Start Next no longer busy")

# ---------------------------------------------------------------------------
# R5 -- live talent: Manual (mic hold) does not stop a VT sequence's speech or
# its incoming start.
# ---------------------------------------------------------------------------
class LiveTalentTests(SimpleTestCase):
    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        outro = fake_voicetrack(self.h.media("outro-vt", seconds=0.8, freq=OUTRO_HZ), 0.8)
        intro = fake_voicetrack(self.h.media("intro-vt", seconds=0.6, freq=INTRO_HZ), 0.6)
        patch_vt_lookup(self, {(1001, "outro"): outro, (1002, "intro"): intro})
        self.fires = spy_fires(self.h)
        items = self.h.queue({"id": 1, "seconds": 8.0}, {"id": 2, "seconds": 8.0})
        set_markers(items[0], outro_starts=1.0)
        self.h.play()

    def test_r5_a_live_mic_suppresses_vt_speech_and_the_handoff_start_has_no_intro_vt(self):
        dc = patch.object(eng_module.DuckingConfig, "load",
                          new=lambda: SimpleNamespace(ptt_auto_manual_enabled=True))
        dc.start()
        self.addCleanup(dc.stop)
        start_poll_loop(self, self.engine)
        self.engine._start_next_track(slot="A")
        pump(0.6)
        self.engine.mic_live = True                 # talent keys the mic -> Manual (mic hold)
        self.engine._apply_mic_mode_hold()
        self.assertTrue(self.engine.manual_mode)
        self.assertTrue(wait_until(lambda: self.engine._manual_hold_pending, 12.0), "the trigger is withheld")
        self.assertEqual(self.h.claims, [1], "nothing starts in Manual with the mic live")
        self.engine.mic_live = False                # mic released -> Auto -> deferred handoff (D5)
        self.engine._apply_mic_mode_hold()
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 3.0))
        pump(1.0)
        fired = [r for r in self.engine.__dict__.get("_vtt_history", ()) if r.fires()]
        live_fires = [f for r in fired for f in r.fires() if f.state in ("admitted", "draining", "ended")]
        self.assertEqual(live_fires, [], "no recorded speech over a live mic; no intro VT on the handoff")
        incoming = next(d for d in self.engine.decks.values() if d is not None and d.log_item.id == 2)
        self.assertIsNone(incoming.scheduled_admission_ns, "the handoff start is ordinary (D5)")

# ---------------------------------------------------------------------------
# R6/R7/R9/R11 -- entry-condition gaps (deterministic positions).
# ---------------------------------------------------------------------------
class EntryConditionTests(SimpleTestCase):
    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        self.outro = fake_voicetrack(self.h.media("outro-vt", seconds=0.8, freq=OUTRO_HZ), 0.8)
        self.intro = fake_voicetrack(self.h.media("intro-vt", seconds=0.6, freq=INTRO_HZ), 0.6)
        self.fires = spy_fires(self.h)
        for name in POLL_STUBS:
            setattr(self.engine, name, lambda *_args, **_kwargs: None)

    def test_r6_outro_marker_at_or_after_the_segue_is_suppressed_without_delay(self):
        # r0144: the ordinary segue fired first and the outro VT was lost
        # silently. Now (D7): suppressed with a reason, segue unchanged.
        self.lookups = patch_vt_lookup(self, {(1001, "outro"): self.outro})
        items = self.h.queue({"id": 1, "seconds": 8, "next_start": 25.0}, {"id": 2, "seconds": 8})
        set_markers(items[0], duration=30.0, outro_starts=26.0)
        self.h.play()
        deck = self.engine._start_next_track(slot="A") or self.engine.decks["A"]
        pump(0.3)
        side = self.engine._vtt_outro_side(deck, eng_module._vtt_track_markers(deck.track), None,
                                           self.engine._vtt_live_context(), self.engine._vt2_running_ns())
        self.assertEqual(side[3], vt_transition.Reason.OUTRO_MARKER_AT_OR_AFTER_SEGUE)
        poll_at(self.engine, deck, 25.0)
        poll_at(self.engine, deck, 26.5)
        self.assertEqual(self.h.claims, [1, 2], "the ordinary segue is not delayed")
        self.assertIsNone(self.engine.__dict__.get("_vtt"))
        self.assertFalse(self.engine.__dict__.get("_vtt_history"))

    def test_r9_a_resumed_occurrence_never_replays_an_uncertain_outro_vt(self):
        # r0144: after a restart nothing recorded that the outro aired; the
        # resumed deck fired it again. Now (D13, no migration): the resume
        # evidence's position plus its staleness bound reaches Outro Starts,
        # so delivery is uncertain and the outro is suppressed.
        self.lookups = patch_vt_lookup(self, {(1001, "outro"): self.outro})
        items = self.h.queue({"id": 1, "seconds": 8, "next_start": 7.5}, {"id": 2, "seconds": 8})
        set_markers(items[0], outro_starts=4.0)
        self.engine._resume_item = items[0]
        self.engine._resume_hint = {"track_id": 1001, "log_item_id": 1, "position": 2.5,
                                    "saved_position": 2.5, "state_age_seconds": 0.5}
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.engine.decks["A"] is not None
                                   and self.engine.decks["A"].continuation_reason == "auto_resume"
                                   and self.engine.decks["A"].gated_seek is None, 8.0))
        deck = self.engine.decks["A"]
        for _ in range(8):
            self.engine._vtt_poll(deck)
            pump(0.25)
        side = self.engine._vtt_outro_side(deck, eng_module._vtt_track_markers(deck.track), None,
                                           self.engine._vtt_live_context(), self.engine._vt2_running_ns())
        self.assertEqual(side[3], vt_transition.Reason.OUTRO_DELIVERY_UNCERTAIN)
        self.assertIsNone(self.engine.__dict__.get("_vtt"))
        self.assertIsNone(self.engine._vtt_outro_delivery_state(1))

    def test_r11_attached_but_unplayable_vt_never_takes_over_the_transition(self):
        # r0144: an attached record with no playable media entered VT mode,
        # ducked under no speech and suppressed the segue (F6). Now an attached
        # record is not eligibility: nothing is owned, ducked or delayed.
        unplayable = SimpleNamespace(gain_db=0.0, playable_audio=lambda: None, filepath="",
                                     duration_seconds=None)
        self.lookups = patch_vt_lookup(self, {(1002, "intro"): unplayable, (1001, "outro"): unplayable})
        items = self.h.queue({"id": 1, "seconds": 8, "next_start": 25.0}, {"id": 2, "seconds": 8})
        set_markers(items[0], duration=30.0, outro_starts=20.0)
        self.h.play()
        deck = self.engine._start_next_track(slot="A") or self.engine.decks["A"]
        pump(0.3)
        poll_at(self.engine, deck, 20.5)
        poll_at(self.engine, deck, 25.5)   # the ordinary trigger point passes
        self.assertEqual(self.engine._vt["phase"], "idle")
        self.assertEqual(self.h.claims, [1, 2], "the segue is not suppressed")
        pump(0.5)
        self.assertIsNone(self.engine.__dict__.get("_vtt"))
        self.assertIsNone(self.engine.__dict__.get("_vt2_duck"), "never ducked under no speech")


class ShortOutgoingTests(SimpleTestCase):
    """R7: an outgoing item that ends before the poll may evaluate it (< 5 s)."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        intro = fake_voicetrack(self.h.media("intro-vt", seconds=0.6, freq=INTRO_HZ), 0.6)
        self.lookups = patch_vt_lookup(self, {(1002, "intro"): intro})
        self.fires = spy_fires(self.h)
        items = self.h.queue({"id": 1, "seconds": 3.0}, {"id": 2, "seconds": 6.0})
        set_markers(items[1], intro_until=3.0)
        start_poll_loop(self, self.engine)
        self.h.play()

    def test_r7_the_intro_vt_after_a_short_item_airs(self):
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 6.0))
        pump(1.0)
        self.assertIn((1002, "intro"), self.lookups)
        record = self.engine.__dict__.get("_vtt") or self.engine.__dict__["_vtt_history"][-1]
        self.assertTrue(wait_until(lambda: record.intro_fire is not None and record.intro_fire.state == "ended",
                                   2.0), "F8: the 3 s item's successor airs its intro VT")

# ---------------------------------------------------------------------------
# R8 + timing baselines -- measured on real program audio.
# ---------------------------------------------------------------------------
class TimingMeasurementTests(SimpleTestCase):
    def _harness(self, table, gap_ms=200):
        self.h = TransportHarness(self, appsink=True)
        self.engine = self.h.engine
        self.lookups = patch_vt_lookup(self, table, gap_ms=gap_ms)
        self.fires = spy_fires(self.h)
        self.trigger_running = []
        original = self.engine._start_next_track

        def spy(slot=None):
            self.trigger_running.append(running_seconds(self.engine))
            return original(slot=slot)
        self.engine._start_next_track = spy

    def test_r0_baseline_ordinary_segue_timing(self):
        self._harness({})
        self.h.queue({"id": 1, "seconds": 9.0, "freq": OUT_HZ, "next_start": 8.0},
                     {"id": 2, "seconds": 5.0, "freq": IN_HZ, "lead_silence": 0.25, "cue_in": 0.25})
        start_poll_loop(self, self.engine)
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 14.0))
        pump(2.0)
        out_on = onset(self.h, OUT_HZ)
        in_on = onset(self.h, IN_HZ, after=out_on + 1.0)
        intended = out_on + 8.0          # outgoing media 8.0 = next_start
        report("R0", outgoing_onset=out_on, incoming_audible_onset=in_on,
               segue_error_vs_next_start=in_on - intended,
               trigger_call_vs_outgoing_onset=self.trigger_running[1] - out_on)
        # Recorded baseline, not a contract: r0144's ordinary segue lands after
        # next_start by poll jitter + the incoming deck's uncompensated 0.3 s
        # silence prime + the wall-clock position estimate's creation lag.
        self.assertGreaterEqual(in_on - intended, 0.0)
        self.assertLess(in_on - intended, 1.5)

    def test_r8_an_intro_only_vt_keeps_the_outgoing_tail_and_the_segue(self):
        self._harness({})
        intro = fake_voicetrack(self.h.media("intro-vt", seconds=1.0, freq=INTRO_HZ), 1.0)
        patch_vt_lookup(self, {(1002, "intro"): intro})
        items = self.h.queue({"id": 1, "seconds": 9.0, "freq": OUT_HZ, "next_start": 7.0},
                             {"id": 2, "seconds": 5.0, "freq": IN_HZ})
        set_markers(items[0], outro_starts=6.0)
        set_markers(items[1], intro_until=3.0)
        start_poll_loop(self, self.engine)
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 16.0))
        pump(2.0)
        out_on = onset(self.h, OUT_HZ)
        segue = out_on + 7.0
        ducked_before_speech = [run for run, lvl in self.h.tone_series(OUT_HZ)
                                if out_on + 1.0 <= run < segue - 0.06 and 0.05 < lvl < 0.37]
        vt_on = onset(self.h, INTRO_HZ)
        in_on = onset(self.h, IN_HZ, after=out_on + 1.0)
        report("R8-B4", vt_onset_vs_segue=vt_on - segue, incoming_onset_vs_segue=in_on - segue,
               speechless_ducked_seconds=len(ducked_before_speech) * 0.01)
        self.assertEqual(ducked_before_speech, [], "no speechless duck (r0144: 3.1 s)")
        self.assertLess(abs(vt_on - segue), 0.02, "VT at the segue (r0144: 2.4 s late)")
        self.assertLess(abs(in_on - segue), 0.02, "the Next Start segue is kept")

    def test_r10_vt_fire_head_latency_and_truncation(self):
        """A 150 ms 1200 Hz marker then 700 Hz: how late does a fire become audible
        after the _vt_fire_file call, and how much of its head survives?"""
        self._harness({})
        self.h.play()
        pump(0.5)
        wav = write_segments(self.h.root / "head.wav", [(0.15, MARKER_HZ), (0.85, INTRO_HZ)])
        sources = [("wav", wav)]
        if shutil.which("flac"):
            flac = self.h.root / "head.flac"
            subprocess.run(["flac", "-s", "-f", "-o", str(flac), str(wav)], check=True)
            sources.append(("flac", flac))
        for label, path in sources:
            for attempt in range(4):
                called = running_seconds(self.engine)
                self.engine._vt_fire_file(str(path), 0.0, "intro")
                pump(1.6)
                heard = onset(self.h, MARKER_HZ, after=called - 0.05)
                marker = present_seconds(self.h, MARKER_HZ, start=called - 0.05, end=called + 1.5)
                report("R10", media=label, attempt=attempt,
                       audible_latency=(heard - called) if heard else -1.0,
                       marker_seconds_heard=marker, marker_seconds_in_file=0.15)
                self.assertIsNotNone(heard)

    def test_r10b_intro_vt_and_incoming_music_follow_the_plan(self):
        """r0144 ended the VT one silence prime early (music 0.56 s after the VT
        for a 0.25 s Cue In). The plan admits the Cue In with the speech."""
        self._harness({})
        intro_vt = fake_voicetrack(self.h.media("intro-vt", seconds=1.0, freq=INTRO_HZ), 1.0)
        patch_vt_lookup(self, {(1002, "intro"): intro_vt}, gap_ms=1)
        items = self.h.queue({"id": 1, "seconds": 9.0, "freq": OUT_HZ, "next_start": 6.0},
                             {"id": 2, "seconds": 5.0, "freq": IN_HZ, "lead_silence": 0.25, "cue_in": 0.25})
        set_markers(items[1], intro_until=3.0)
        start_poll_loop(self, self.engine)
        self.h.play()
        self.engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 12.0))
        pump(2.0)
        out_on = onset(self.h, OUT_HZ)
        vt_on = onset(self.h, INTRO_HZ, after=out_on + 1.0)
        in_on = onset(self.h, IN_HZ, after=out_on + 1.0)
        report("R10b-B4", vt_vs_segue=vt_on - (out_on + 6.0), music_minus_vt=in_on - vt_on)
        self.assertLess(abs(vt_on - (out_on + 6.0)), 0.02)
        self.assertLess(abs(in_on - vt_on), 0.02, "music (Cue In) with the speech: U = 0")
