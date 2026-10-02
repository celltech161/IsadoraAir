"""r0103 / 1.19 -- playback position accuracy after seek, pause/resume and restart.

Root cause (measured, not inferred): a FLUSHING seek to media time T starts a
new segment (start=T, base=0), so the first post-seek buffer has RUNNING time
~0. The deck's mixer pad offset was computed as ``running_now - T`` -- treating
the media position as running time -- so post-seek audio reached the mixer T
seconds late and the mixer's catch-up discarded ~T seconds of media: a seek,
resume or restart to T actually played from ~2T (30 s -> ~61 s, 12 min -> ~24
min), and past mid-track that overshoots the end, producing an "anomalous" EOS.

These tests measure the TRUE audio position -- the PTS of decoded buffers
leaving the deck's internal stage -- rather than trusting the engine's own
reported value, which honestly reported the wrong audio.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import gi
from django.test import SimpleTestCase, TransactionTestCase

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from library.services import engine as eng_module
from library.services.engine import PlaybackEngine, _segment_running_time_ns
from library.tests.test_engine_deck_lifecycle import _make_log_item, _make_track, _write_wav
from library.tests.test_engine_eos_plausibility import _pump_engine, _settle_teardowns_then_stop
from library.tests.test_engine_seek_audio_flow import _make_real_engine_clocked

Gst.init(None)
SECOND = Gst.SECOND
# Seek accuracy + up to one buffer of startup; small enough that the old ~2x
# error (tens of seconds) and even a 1 s regression fail loudly.
TOLERANCE_S = 0.5


def _segment(start_ns, *, base_ns=0, fmt=Gst.Format.TIME):
    segment = Gst.Segment()
    segment.init(fmt)
    segment.start = start_ns
    segment.time = start_ns
    segment.base = base_ns
    return segment


def _pad_with_segment(segment):
    event = Gst.Event.new_segment(segment) if segment is not None else None
    return SimpleNamespace(get_sticky_event=lambda _type, _idx: event)


class SegmentRunningTimeTests(SimpleTestCase):
    """The pure mapping the fix relies on, using GStreamer's own segment math."""

    def test_after_a_flushing_seek_the_target_has_running_time_zero_not_its_position(self):
        pad = _pad_with_segment(_segment(30 * SECOND))
        self.assertEqual(_segment_running_time_ns(pad, 30 * SECOND), 0)
        self.assertEqual(_segment_running_time_ns(pad, 31 * SECOND), 1 * SECOND)

    def test_a_long_seek_still_maps_to_running_time_zero(self):
        pad = _pad_with_segment(_segment(720 * SECOND))
        self.assertEqual(_segment_running_time_ns(pad, 720 * SECOND), 0)

    def test_an_unseeked_stream_keeps_running_time_equal_to_position(self):
        pad = _pad_with_segment(_segment(0))
        self.assertEqual(_segment_running_time_ns(pad, 12 * SECOND), 12 * SECOND)

    def test_segment_base_is_honoured(self):
        pad = _pad_with_segment(_segment(30 * SECOND, base_ns=5 * SECOND))
        self.assertEqual(_segment_running_time_ns(pad, 30 * SECOND), 5 * SECOND)

    def test_position_before_segment_start_maps_to_negative_running_time(self):
        # KEY_UNIT seeks can deliver a buffer slightly before the segment start.
        pad = _pad_with_segment(_segment(30 * SECOND, base_ns=1 * SECOND))
        self.assertEqual(_segment_running_time_ns(pad, 29_500_000_000), 500_000_000)

    def test_no_usable_segment_fails_closed(self):
        self.assertIsNone(_segment_running_time_ns(_pad_with_segment(None), 30 * SECOND))
        self.assertIsNone(_segment_running_time_ns(None, 30 * SECOND))
        self.assertIsNone(_segment_running_time_ns(_pad_with_segment(_segment(0)), None))
        bytes_pad = _pad_with_segment(_segment(0, fmt=Gst.Format.BYTES))
        self.assertIsNone(_segment_running_time_ns(bytes_pad, 30 * SECOND))


def _make_sine_flac(path, seconds):
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=22050",
            "-c:a", "flac", str(path),
        ],
        check=True, timeout=60,
    )


class PositionAccuracyFixture(TransactionTestCase):
    """Real GStreamer decks on a real-time (sync=True) clocked mixer."""

    media_seconds = 180.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="isadoraair-1-19.")
        self.addCleanup(self.tmp.cleanup)
        self.path = self.make_media(Path(self.tmp.name))
        self.track = _make_track(self.path, track_id=1, duration=self.media_seconds, title="1.19")
        self.item = _make_log_item(self.track, item_id=17)
        self.item.played_at = object()
        self.engine = _make_real_engine_clocked()
        self.engine._claim_playback_occurrence = lambda _item: True
        self.addCleanup(lambda: self.engine.main_pipeline.set_state(Gst.State.NULL))
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

    def make_media(self, directory):
        path = directory / "media.wav"
        _write_wav(path, frames=int(self.media_seconds * 44100))
        return path

    # -- measurement helpers ------------------------------------------------
    def watch_audio(self, deck):
        """Track the PTS of decoded buffers leaving the deck: the true media
        position, independent of anything the engine computes."""
        state = {"pts": None}
        pad = deck.seek_gate_pad or deck.pipeline.get_static_pad("src").get_target()

        def probe(_pad, info):
            buf = info.get_buffer()
            if buf is not None and buf.pts != Gst.CLOCK_TIME_NONE:
                state["pts"] = buf.pts
            return Gst.PadProbeReturn.OK

        pad.add_probe(Gst.PadProbeType.BUFFER, probe)
        return state

    def audio_seconds(self, state):
        self.assertIsNotNone(state["pts"], "no decoded audio observed")
        return state["pts"] / SECOND

    def resolve(self, deck):
        self.assertTrue(_pump_engine(self.engine, lambda: deck.gated_seek is None, timeout=15.0))
        self.assertIs(self.engine.decks[deck.slot], deck, "seek fell back instead of resolving")

    def resume_at(self, seconds):
        """The real restart route: a validated resume hint consumed by
        _create_deck, which performs the gated auto-resume seek."""
        if self.engine.decks.get("A") is not None:
            self.engine._remove_deck(self.engine.decks["A"])
            self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks.get("A") is None, timeout=10.0))
            # Let the slot's bounded teardown worker go idle before the next generation.
            self.assertTrue(_pump_engine(
                self.engine, lambda: self.engine._deck_slot_available("A"), timeout=10.0,
            ))
        self.engine._resume_hint = {
            "track_id": self.track.id, "log_item_id": self.item.id,
            "position": seconds, "saved_position": seconds + 0.25, "state_age_seconds": 0.5,
        }
        deck = self.engine._create_deck("A", self.item)
        self.assertEqual(deck.continuation_reason, "auto_resume")
        self.resolve(deck)
        return deck

    def play_for(self, seconds):
        _pump_engine(self.engine, lambda: False, timeout=seconds)

    def assert_near(self, actual, expected, what):
        self.assertAlmostEqual(
            actual, expected, delta=TOLERANCE_S,
            msg=f"{what}: expected ~{expected:.2f}s, got {actual:.2f}s (error {actual - expected:+.2f}s)",
        )


class SeekResumeAccuracyTests(PositionAccuracyFixture):
    def test_restart_resume_at_30s_plays_from_30s_not_60s(self):
        deck = self.resume_at(30.0)
        audio = self.watch_audio(deck)
        started = time.monotonic()
        self.play_for(2.0)
        elapsed = time.monotonic() - started
        self.assert_near(self.audio_seconds(audio), 30.0 + elapsed, "true audio after resume@30")
        self.assert_near(self.engine._get_deck_position(deck), 30.0 + elapsed, "reported after resume@30")

    def test_manual_seek_to_t_plays_from_t_and_reports_t(self):
        deck = self.engine._create_deck("A", self.item)
        self.assertTrue(_pump_engine(self.engine, lambda: deck.media_buffer_count > 0, timeout=5.0))
        self.play_for(1.0)
        self.engine._seek_deck("A", 45.0)
        deck = self.engine.decks["A"]
        self.resolve(deck)
        audio = self.watch_audio(deck)
        started = time.monotonic()
        self.play_for(2.0)
        elapsed = time.monotonic() - started
        self.assert_near(self.audio_seconds(audio), 45.0 + elapsed, "true audio after seek@45")
        self.assert_near(self.engine._get_deck_position(deck), 45.0 + elapsed, "reported after seek@45")

    def test_pause_does_not_advance_and_resume_continues_from_the_paused_position(self):
        deck = self.engine._create_deck("A", self.item)
        self.assertTrue(_pump_engine(self.engine, lambda: deck.media_buffer_count > 0, timeout=5.0))
        self.play_for(6.0)
        self.engine._pause_deck("A")
        paused = self.engine._get_deck_position(deck)
        self.play_for(3.0)  # wall-clock time spent paused
        self.assertEqual(self.engine._get_deck_position(deck), paused, "position advanced while paused")
        self.engine._resume_deck("A")
        deck = self.engine.decks["A"]
        self.resolve(deck)
        audio = self.watch_audio(deck)
        started = time.monotonic()
        self.play_for(2.0)
        elapsed = time.monotonic() - started
        # Old behaviour: ~2x the paused position. Pause duration never counts.
        self.assert_near(self.audio_seconds(audio), paused + elapsed, "true audio after resume")

    def test_repeated_restart_recovery_never_compounds(self):
        """Snapshot -> sanitize -> resume, three times, through the real chain."""
        deck = self.resume_at(30.0)
        self.play_for(1.0)
        history = []
        for cycle in range(3):
            persisted = self.engine._get_deck_position(deck)            # what _write_state saves
            target, policy = PlaybackEngine._sanitize_resume_position(  # what the next process seeks to
                SimpleNamespace(track=self.track), persisted,
            )
            deck = self.resume_at(target)
            audio = self.watch_audio(deck)
            started = time.monotonic()
            self.play_for(1.0)
            elapsed = time.monotonic() - started
            observed = self.audio_seconds(audio)
            history.append((round(persisted, 2), target, round(observed, 2)))
            self.assertEqual(policy, "saved_position_with_overlap")
            self.assert_near(observed, target + elapsed, f"cycle {cycle} recovered audio")
        # Each recovery is anchored to the previous true position: ~1 s of real
        # play per cycle minus the 0.25 s overlap, never a doubling walk.
        self.assertLess(history[-1][2], 30.0 + 3 * 1.5 + 1.5, history)

    def test_recovery_without_further_playback_is_idempotent(self):
        deck = self.resume_at(30.0)
        persisted = self.engine._get_deck_position(deck)
        for _cycle in range(3):
            deck = self.resume_at(persisted)
            persisted_again = self.engine._get_deck_position(deck)
            self.assert_near(persisted_again, persisted, "re-recovered position")
            persisted = persisted_again


class LongTrackRestartAccuracyTests(PositionAccuracyFixture):
    """The KOGR 12-minute case on a 15-minute FLAC: doubling would land at
    ~24 min -- past the end of the file -- and end the deck immediately."""

    media_seconds = 900.0

    def make_media(self, directory):
        path = directory / "show.flac"
        _make_sine_flac(path, int(self.media_seconds))
        return path

    def test_restart_at_twelve_minutes_resumes_at_twelve_minutes(self):
        deck = self.resume_at(720.0)
        audio = self.watch_audio(deck)
        started = time.monotonic()
        self.play_for(2.0)
        elapsed = time.monotonic() - started
        self.assert_near(self.audio_seconds(audio), 720.0 + elapsed, "true audio after resume@12min")
        self.assertIs(self.engine.decks["A"], deck, "deck ended (EOS) after the long resume")
        self.assertFalse(deck.finished)


class FlacPastMidpointSeekEosTests(PositionAccuracyFixture):
    """The WRJE TLC FLAC "anomalous EOS after seek/recovery": with the old
    offset, seeking past the midpoint doubled beyond the end of the media."""

    media_seconds = 60.0

    def make_media(self, directory):
        path = directory / "tlc-like.flac"
        _make_sine_flac(path, int(self.media_seconds))
        return path

    def test_seek_past_midpoint_keeps_playing_instead_of_ending(self):
        deck = self.engine._create_deck("A", self.item)
        self.assertTrue(_pump_engine(self.engine, lambda: deck.media_buffer_count > 0, timeout=5.0))
        self.engine._seek_deck("A", 40.0)  # old behaviour: ~80 s > 60 s -> EOS
        deck = self.engine.decks["A"]
        self.resolve(deck)
        audio = self.watch_audio(deck)
        started = time.monotonic()
        self.play_for(3.0)
        elapsed = time.monotonic() - started
        self.assertIs(self.engine.decks["A"], deck, "deck ended right after a past-midpoint seek")
        self.assertFalse(deck.finished)
        self.assert_near(self.audio_seconds(audio), 40.0 + elapsed, "true audio after FLAC seek@40")

    def test_resume_past_midpoint_keeps_playing_instead_of_ending(self):
        deck = self.resume_at(45.0)
        audio = self.watch_audio(deck)
        started = time.monotonic()
        self.play_for(3.0)
        elapsed = time.monotonic() - started
        self.assertIs(self.engine.decks["A"], deck)
        self.assertFalse(deck.finished)
        self.assert_near(self.audio_seconds(audio), 45.0 + elapsed, "true audio after FLAC resume@45")


class FlacFormatRenegotiationTests(PositionAccuracyFixture):
    """The second FLAC mechanism: media whose native format differs from the
    deck mixer's (here 22.05 kHz mono). Releasing the predecessor's mixer pad
    mid-seek let the mixer renegotiate while post-seek buffers were in flight,
    and the parser stopped with not-negotiated -> deck ERROR right after a
    verified seek (reproduced on exact r0102 too, 1/8 at 25 s; 6/8 once timing
    was correct). Seek/resume generations now carry the fixed pipeline-format
    output caps a fresh deck already has."""

    media_seconds = 60.0

    def make_media(self, directory):
        path = directory / "mono-22k.flac"
        _make_sine_flac(path, int(self.media_seconds))
        return path

    def test_repeated_seeks_from_a_playing_deck_never_error_or_end(self):
        outcomes = []
        for target in (25.0, 40.0, 28.0, 45.0, 25.0, 40.0):
            if self.engine.decks.get("A") is not None:
                self.engine._remove_deck(self.engine.decks["A"])
                self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks.get("A") is None, timeout=10.0))
            self.assertTrue(_pump_engine(self.engine, lambda: self.engine._deck_slot_available("A"), timeout=10.0))
            fresh = self.engine._create_deck("A", self.item)
            self.assertTrue(_pump_engine(self.engine, lambda: fresh.media_buffer_count > 0, timeout=5.0))
            self.engine._seek_deck("A", target)
            deck = self.engine.decks["A"]
            self.resolve(deck)
            self.play_for(1.0)
            outcomes.append((target, deck.completion_reason, self.engine.decks.get("A") is deck))
        self.assertEqual(
            [o for o in outcomes if o[1] is not None or not o[2]], [],
            f"decks ended/errored after a verified seek: {outcomes}",
        )

    def test_seek_deck_output_is_pinned_to_the_pipeline_format(self):
        deck = self.engine._create_deck("A", self.item)
        self.assertTrue(_pump_engine(self.engine, lambda: deck.media_buffer_count > 0, timeout=5.0))
        self.engine._seek_deck("A", 20.0)
        deck = self.engine.decks["A"]
        self.resolve(deck)
        caps = deck.pipeline.get_static_pad("src").get_current_caps().get_structure(0)
        self.assertEqual(caps.get_value("rate"), self.engine.pipeline_sample_rate)
        self.assertEqual(caps.get_value("channels"), 2)


class PostSeekDiagnosticsTests(SimpleTestCase):
    def test_accepted_seek_log_line_carries_the_exact_timing_fields(self):
        import inspect
        src = inspect.getsource(PlaybackEngine._resolve_gated_seek)
        for field in ("requested_seek_ns=", "confirmed_position_ns=", "segment_running_time_ns=", "track_id="):
            self.assertIn(field, src)
        self.assertIs(eng_module._segment_running_time_ns, _segment_running_time_ns)
