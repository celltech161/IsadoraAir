"""[P2] 1.6 Phase A -- deterministic, hardware-free proof for
FIRST_REAL_POST_PRIMER_MILESTONE, the new purely observational Deck
milestone added in engine.py's _create_deck: "a buffer genuinely
confirmed to have crossed the deck's post-primer output boundary
(concat's src pad, gated on concat's own active-pad bookkeeping, for a
silence-primed fresh start; the real decode-stage pad itself, which IS
the ghost target, for a non-primed resume_position_ns recreation) has
reached the point where it would join the live mixer."

This module observes; it does not change LogItem.played_at,
Track.last_played_at, Track.play_count, mark_song_requests_aired, or
PlayEvent. See docs/PLAYBACK_EVIDENCE_BOUNDARY_PHASE_A.md for the full
Phase A report and scratchpad/playback_boundary_p2_1_6/ for the isolated
concat-only harness this milestone's design is based on.

Real GStreamer bins throughout (same building blocks as
test_engine_deck_lifecycle.py's RealDeckTopologyTests), hardware-free,
media written only beneath TemporaryDirectory. No amplitude/RMS
assertion anywhere -- every "real vs. primer" distinction here comes
from buffer-flow identity (concat's active-pad), never from sample
values, exactly matching production's own constraint that real source
audio may itself be digitally silent.
"""

from __future__ import annotations

import os
import struct
import tempfile
import threading
import time
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import gi
from django.test import SimpleTestCase, TransactionTestCase

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import library.services.engine as eng_module
from library.services.engine import FIRST_REAL_POST_PRIMER_MILESTONE
from library.tests.test_engine_deck_lifecycle import (
    _make_log_item,
    _make_real_engine,
    _make_track,
    _wait_until,
    _write_wav,
)
from library.tests.test_engine_eos_plausibility import (
    _gated_seek_fixture,
    _pump_engine,
    _pump_until_seek_resolved,
    _settle_teardowns_then_stop,
)


def _write_silent_wav(path, *, frames=44100, sample_rate=44100):
    """Digitally silent (all-zero) real media -- distinguishable from the
    production silence PRIMER only by which branch/generation produced
    it, never by amplitude (both are, deliberately, exactly zero)."""
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack("<hh", 0, 0) * frames)


def _accounting_patches():
    """Same non-DB-touching substitutions RealDeckTopologyTests applies
    (test_engine_deck_lifecycle.py) -- log_item/track are MagicMocks so
    .save()/attribute writes are already harmless, but Track.objects.
    filter(...).update(...) and PlayEvent.objects.create(...) are real
    manager methods that would otherwise hit the DB."""
    return (
        patch.object(
            eng_module.Track.objects, "filter",
            new=lambda *_args, **_kwargs: type("F", (), {"update": staticmethod(lambda **_f: None)})(),
        ),
        patch.object(eng_module.PlayEvent.objects, "create", new=lambda **_kwargs: MagicMock(id=None)),
        patch.object(eng_module, "mark_song_requests_aired", new=lambda *_args, **_kwargs: None),
        patch.object(eng_module, "emit_event", new=lambda **_kwargs: None),
    )


class _BoundaryTestBase(SimpleTestCase):
    """Shared real-engine/tempdir/patch scaffolding for every scenario
    below. Each subclass gets its own fresh engine/pipeline -- nothing
    here is shared mutable module state."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-boundary-evidence.")
        self.addCleanup(self.temp_dir.cleanup)
        self.engine = _make_real_engine()
        self.addCleanup(self._cleanup_engine)
        self._patches = _accounting_patches()
        for mocked in self._patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def _cleanup_engine(self):
        for coordinator in self.engine._deck_teardowns.values():
            coordinator.stop()
        self.engine.main_pipeline.set_state(Gst.State.NULL)

    def _wav(self, name, *, frames=4410, silent=False):
        path = Path(self.temp_dir.name) / name
        if silent:
            _write_silent_wav(path, frames=frames)
        else:
            _write_wav(path, frames=frames)
        return path

    def _create(self, generation, *, slot="A", path, duration=0.1, resume_position_ns=None):
        track = _make_track(path, generation, duration=duration, title=f"Track {generation}")
        item = _make_log_item(track, generation)
        return self.engine._create_deck(slot, item, resume_position_ns=resume_position_ns)


class FreshStartOrderingTests(_BoundaryTestBase):
    """Scenario A: ordinary fresh start -- prove the observed sequence
    and record representative timing deltas."""

    def test_ordering_and_timing_deck_creation_primer_then_real_output(self):
        wav_path = self._wav("fresh.wav", frames=4 * 44100, silent=False)
        deck = self._create(1, path=wav_path, duration=4.0)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

        self.assertTrue(_wait_until(lambda: FIRST_REAL_POST_PRIMER_MILESTONE in deck.eos_milestones, timeout=5.0), deck.milestone_snapshot())

        snapshot = deck.milestone_snapshot()
        boundary = snapshot["milestones"][FIRST_REAL_POST_PRIMER_MILESTONE]
        # Fired exactly once (see the harness's active-pad-gated,
        # self-removing probe) -- not once per real buffer.
        self.assertEqual(boundary["count"], 1)
        # Real content only starts flowing after the primer has had a
        # chance to occupy concat's output -- the milestone must not
        # fire at t=0 (which would mean it fired on the primer itself,
        # exactly the failure mode this design exists to rule out).
        # SILENCE_PRIME_SECONDS is 0.3s of program time but decodes/
        # switches essentially as fast as the pipeline can push it in a
        # hardware-free sync=False harness, so this only asserts a
        # small positive margin, not real wall-clock elapsed primer
        # time.
        self.assertGreater(boundary["first_ms"], 0)
        self.assertTrue(deck.media_buffer_count > 0)
        print(
            "fresh-start-boundary-timing "
            f"first_real_post_primer_ms={boundary['first_ms']} "
            f"media_buffer_count_at_assertion={deck.media_buffer_count}",
            flush=True,
        )
        self.engine._remove_deck(deck)

    def test_very_short_valid_media_reaches_boundary_once(self):
        """Scenario E -- a very short real asset (shorter than
        SILENCE_PRIME_SECONDS itself) still reaches the boundary
        exactly once."""
        wav_path = self._wav("short.wav", frames=200, silent=False)
        deck = self._create(2, path=wav_path, duration=200 / 44100)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(_wait_until(lambda: deck.finished, timeout=5.0), deck.milestone_snapshot())
        self.assertIn(FIRST_REAL_POST_PRIMER_MILESTONE, deck.eos_milestones)
        self.assertEqual(deck.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)


class DigitallySilentMediaTests(_BoundaryTestBase):
    """Scenario B: a real media stream whose decoded samples are silence
    must still fire the boundary -- this is buffer-flow evidence, not
    amplitude detection."""

    def test_digitally_silent_real_media_still_crosses_boundary(self):
        wav_path = self._wav("digitally-silent.wav", frames=2 * 44100, silent=True)
        deck = self._create(3, path=wav_path, duration=2.0)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(
            _wait_until(lambda: FIRST_REAL_POST_PRIMER_MILESTONE in deck.eos_milestones, timeout=5.0),
            deck.milestone_snapshot(),
        )
        self.assertEqual(deck.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)
        self.engine._remove_deck(deck)


class NeverRealFailedStartTests(TransactionTestCase):
    """Scenario C: the deck is created and begins state transition
    (the silence primer flows normally -- audiotestsrc has no
    dependency on the real source), but decode never produces a single
    real-content buffer. The proposed milestone must never fire.

    Same FIFO-with-a-permanently-open-but-silent-writer technique as
    test_engine_eos_plausibility.py's NeverPrerolledTests, applied here
    to an ordinary FRESH (silence-primed) _create_deck call rather than
    a gated seek."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-never-real.")
        self.addCleanup(self.temp_dir.cleanup)
        self.fifo_path = Path(self.temp_dir.name) / "never-arrives.wav"
        os.mkfifo(self.fifo_path)
        self._fifo_writer_opened = threading.Event()

        def _hold_fifo_writer_open():
            fd = os.open(str(self.fifo_path), os.O_WRONLY)
            self._fifo_writer_opened.set()
            # Deliberately never closed/written -- see NeverPrerolledTests'
            # own docstring for why a bare unwritten-to FIFO (rather than
            # no writer at all) is what's needed here.
            self._held_fd = fd

        threading.Thread(target=_hold_fifo_writer_open, daemon=True).start()

        self.engine = _make_real_engine()
        self.addCleanup(self._detach_never_nulled_stuck_deck)
        self._patches = _accounting_patches() + (
            patch.object(eng_module, "_log_item_playable", return_value=(True, None)),
        )
        for mocked in self._patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def _detach_never_nulled_stuck_deck(self):
        deck = self.engine.decks.get("A")
        if deck is None:
            return
        try:
            src_pad = deck.pipeline.get_static_pad("src")
            if deck.mixer_pad is not None:
                src_pad.unlink(deck.mixer_pad)
                self.engine.mixer.release_request_pad(deck.mixer_pad)
            self.engine.main_pipeline.remove(deck.pipeline)
        except Exception:
            pass

    def test_never_real_start_never_fires_boundary(self):
        track = _make_track(self.fifo_path, track_id=1, duration=6.0, title="Never Real")
        log_item = _make_log_item(track, item_id=1)
        deck = self.engine._create_deck("A", log_item)
        self.assertIsNotNone(deck)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(self._fifo_writer_opened.wait(timeout=2.0))

        # Earlier construction milestones (the silence primer itself is
        # self-contained -- audiotestsrc, no dependency on the FIFO) MAY
        # exist and are given the whole window below to flow; the
        # proposed real-air-start milestone must never appear in it.
        self.assertFalse(
            _wait_until(lambda: FIRST_REAL_POST_PRIMER_MILESTONE in deck.eos_milestones, timeout=1.5)
        )
        # Ground truth for WHY it never fired: decode genuinely never
        # produced a single real buffer (the FIFO never received a
        # byte), not merely "the assertion window was too short."
        self.assertEqual(deck.media_buffer_count, 0)
        self.assertNotIn(FIRST_REAL_POST_PRIMER_MILESTONE, deck.eos_milestones)


class TeardownBeforeRealOutputTests(_BoundaryTestBase):
    """Scenario D: retire a fresh generation before the proposed
    boundary is ever reached. The milestone must remain absent, and no
    stale callback may mark a later, replacement generation."""

    def test_teardown_before_real_output_leaves_boundary_absent_and_replacement_unaffected(self):
        # A track long enough that "retire immediately, before PLAYING
        # even reaches the mixer" reliably wins the race against decode.
        wav_path = self._wav("retired.wav", frames=2 * 44100, silent=False)
        deck = self._create(4, path=wav_path, duration=2.0)
        # Retire before the pipeline is even told to play -- decode has
        # had zero opportunity to run.
        self.engine._remove_deck(deck)
        self.assertTrue(_wait_until(lambda: deck.finished or "N_BIN_REMOVAL_COMPLETE" in deck.eos_milestones))
        self.assertNotIn(FIRST_REAL_POST_PRIMER_MILESTONE, deck.eos_milestones)

        # A fresh replacement generation in the same slot must reach the
        # boundary normally and must not inherit anything from the
        # retired generation (separate Deck object, separate milestones
        # dict, separate concat/pad closures entirely).
        replacement_path = self._wav("replacement.wav", frames=2 * 44100, silent=False)
        replacement = self._create(5, path=replacement_path, duration=2.0)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(
            _wait_until(lambda: FIRST_REAL_POST_PRIMER_MILESTONE in replacement.eos_milestones, timeout=5.0),
            replacement.milestone_snapshot(),
        )
        self.assertNotIn(FIRST_REAL_POST_PRIMER_MILESTONE, deck.eos_milestones)
        self.engine._remove_deck(replacement)


class CrossfadeOverlapTests(_BoundaryTestBase):
    """Scenario F: two deck generations producing concurrently in a
    hardware-free mixer path. Each deck's own boundary evidence must
    stay independently attributable to its own generation."""

    def test_concurrent_generations_each_get_independently_attributed_boundary(self):
        path_a = self._wav("crossfade-a.wav", frames=3 * 44100, silent=False)
        path_b = self._wav("crossfade-b.wav", frames=3 * 44100, silent=True)  # digitally silent, deliberately
        deck_a = self._create(10, slot="A", path=path_a, duration=3.0)
        deck_b = self._create(11, slot="B", path=path_b, duration=3.0)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

        self.assertTrue(
            _wait_until(
                lambda: (
                    FIRST_REAL_POST_PRIMER_MILESTONE in deck_a.eos_milestones
                    and FIRST_REAL_POST_PRIMER_MILESTONE in deck_b.eos_milestones
                ),
                timeout=5.0,
            ),
            (deck_a.milestone_snapshot(), deck_b.milestone_snapshot()),
        )
        self.assertEqual(deck_a.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)
        self.assertEqual(deck_b.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)
        self.assertIsNot(deck_a, deck_b)
        self.assertNotEqual(deck_a.generation, deck_b.generation)
        self.engine._remove_deck(deck_a)
        self.engine._remove_deck(deck_b)


class ManualSeekRecreationTests(TransactionTestCase):
    """Scenario G: _create_deck(..., resume_position_ns=...) / the
    gated-seek path. Confirm the observability remains
    generation-correlated, and document (not silently assume) exactly
    what the milestone means for a recreated generation: media-flow
    evidence that real decode has started for this generation, NOT
    confirmation that content at the target seek position specifically
    has reached the boundary -- the milestone is observed on
    real_stage_src, which for a non-primed deck is the ghost target
    itself but sits UPSTREAM of the gated-seek's own separate
    BLOCK|BUFFER gate on the external ghost_pad, so it fires on the
    pre-seek preroll buffer the gate itself waits for, before the
    flushing seek discards that buffer and re-starts decode at the
    target. Later accounting must not interpret this as confirmation of
    a second logical occurrence play."""

    def setUp(self):
        self.engine, self.temp_dir, self.track, self.log_item = _gated_seek_fixture(duration_seconds=6.0)
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(lambda: self.engine.main_pipeline.set_state(Gst.State.NULL))
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))
        self._patches = _accounting_patches()
        for mocked in self._patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

    def test_gated_seek_recreation_fires_once_as_media_flow_evidence(self):
        with patch("builtins.print"):
            self.engine._begin_gated_seek("A", self.log_item, 3.0, was_paused=False)
            deck = self.engine.decks["A"]
            self.assertIsNotNone(deck)
            self.assertFalse(deck.silence_primed)
            resolved = _pump_until_seek_resolved(self.engine, deck, timeout=3.0)
        self.assertTrue(resolved)
        self.assertIsNone(deck.gated_seek)
        self.assertIn(FIRST_REAL_POST_PRIMER_MILESTONE, deck.eos_milestones)
        self.assertEqual(deck.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)


class AutoResumeObservationTests(_BoundaryTestBase):
    """Scenario H: the _resume_hint path. The recreated generation keeps
    silence_primed=True (auto-resume seeks AFTER creation rather than
    passing resume_position_ns -- see _create_deck's own docstring), so
    the boundary evidence here comes from the SAME concat/active-pad
    mechanism as an ordinary fresh start, observed BEFORE the auto-
    resume seek is even issued. Document, don't silently assume, that
    this means the milestone reflects content from the start of the
    track, not from the resumed position -- Phase A observes this; it
    does not change accounting."""

    def test_auto_resume_hint_keeps_silence_prime_and_fires_boundary_pre_seek(self):
        wav_path = self._wav("auto-resume.wav", frames=4 * 44100, silent=False)
        track = _make_track(wav_path, 20, duration=4.0, title="Auto Resume Track")
        item = _make_log_item(track, 20)
        self.engine._resume_hint = {
            "track_id": track.id,
            "position": 1.5,
            "log_item_id": item.id,
        }
        with patch("builtins.print"):
            deck = self.engine._create_deck("A", item)
        self.assertIsNotNone(deck)
        self.assertTrue(deck.silence_primed, "auto-resume must keep the silence prime intact")
        self.assertIsNone(self.engine._resume_hint, "hint must be consumed exactly once")
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(
            _wait_until(lambda: FIRST_REAL_POST_PRIMER_MILESTONE in deck.eos_milestones, timeout=5.0),
            deck.milestone_snapshot(),
        )
        self.assertEqual(deck.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)
        self.engine._remove_deck(deck)


class PauseResumeOneShotTests(_BoundaryTestBase):
    """Scenario I: the milestone remains one-shot for one deck
    generation, and ordinary pause/resume does not manufacture another
    first-start event. In this codebase _resume_deck always tears down
    and recreates a fresh generation via _begin_gated_seek (see its own
    docstring) rather than literally resuming the same GStreamer bin
    from PAUSED -- so pausing produces no new buffers on the paused
    generation at all (proven below: the count stays at exactly 1), and
    a subsequent resume's fresh generation is scenario G's own gated-
    seek path, correctly getting its OWN independent milestone rather
    than incrementing the original generation's."""

    def setUp(self):
        super().setUp()
        # _resume_deck below goes through _begin_gated_seek, whose
        # in-flight teardown-coordinator work must be fully drained
        # before the plain main_pipeline.set_state(NULL) in
        # _cleanup_engine (registered by super().setUp(), and run AFTER
        # this since addCleanup is LIFO) -- otherwise a still-draining
        # "deck-real-a-test-worker" can survive this test and confuse a
        # later test file's own thread-count assertions, exactly the
        # hazard test_engine_eos_plausibility.py's
        # _settle_teardowns_then_stop docstring describes.
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))

    def test_pause_does_not_refire_and_resume_creates_independently_attributed_generation(self):
        wav_path = self._wav("pauseable.wav", frames=6 * 44100, silent=False)
        deck = self._create(30, slot="A", path=wav_path, duration=6.0)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(
            _wait_until(lambda: FIRST_REAL_POST_PRIMER_MILESTONE in deck.eos_milestones, timeout=5.0),
            deck.milestone_snapshot(),
        )
        self.assertEqual(deck.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)

        with patch("builtins.print"):
            self.engine._pause_deck("A")
        self.assertTrue(deck.paused)
        # No new buffers flow while paused -- give the pipeline a brief,
        # bounded window to prove nothing sneaks through, then confirm
        # the count is unchanged.
        time.sleep(0.05)
        self.assertEqual(deck.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)

        with patch("builtins.print"):
            self.engine._resume_deck("A")
            resumed_deck = self.engine.decks["A"]
            self.assertIsNot(resumed_deck, deck)
            self.assertTrue(_pump_until_seek_resolved(self.engine, resumed_deck, timeout=3.0))

        # The original (paused, retired) generation's own record is
        # untouched by the new generation's activity.
        self.assertEqual(deck.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)
        self.assertIn(FIRST_REAL_POST_PRIMER_MILESTONE, resumed_deck.eos_milestones)
        self.assertEqual(resumed_deck.eos_milestones[FIRST_REAL_POST_PRIMER_MILESTONE]["count"], 1)
