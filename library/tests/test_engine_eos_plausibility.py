"""Regression coverage for the spurious-EOS-after-seek bug: a flushing
seek into compressed audio (the auto-resume seek on an engine restart,
or a manual seek from the wave-canvas UI -- _seek_deck/_resume_deck use
the identical FLUSH|KEY_UNIT pattern) can land on a byte offset the
parser can't cleanly resync from, producing a spurious EOS event
through the deck's own eos_probe pad probe. Blindly trusting that EOS
tore the deck down and rebuilt it from position 0, discarding whatever
resume/seek position had just been applied -- observed live
2026-07-31/08-01 as a listener-facing bug: a track audibly restarted
from the beginning partway through an engine restart's auto-resume.

_on_deck_eos_probed checks the deck's actual position against its
track's recorded duration before trusting an EOS as genuine, but ONLY
within a short window (SEEK_EOS_GUARD_SECONDS) after an actual seek was
applied (Deck.seeked_at) -- an unseeked deck's EOS is trusted exactly
as it always was, deliberately narrowing the risk surface to just the
scenario there's independent reason to suspect. The margin itself is
capped at half the track's own duration so it can never go negative
(and so silently disable the whole check) for anything shorter than
DECK_STUCK_TIMEOUT_SECONDS -- a real bug in the first version of this
fix, caught in review: station IDs, sweepers, WxAlert/UrgentPA inserts,
and dedication intros are all shorter than that 30s margin.

Root cause verified via an isolated throwaway-pipeline reproduction
(zero connection to the live engine, same file/seek offset as the
incident) rather than guessed: the gst_base_parse_finish_frame
assertion is 100% deterministic for that seek, but the pipeline itself
recovered cleanly every time (5/5 runs) and resumed normal real-time
decoding within ~2.7s. SEEK_EOS_GUARD_SECONDS=5.0 deliberately pads
past that observed floor -- the isolated repro carried none of a real
restart's concurrent startup load, so it likely underestimates the
real settle time.

Also covers a second bug caught in the same review round:
_create_deck sets Deck.started_at from resume_position_ns
unconditionally, as presentation bookkeeping -- it never itself seeks
for that path, the caller (_resume_deck/_seek_deck) does, afterward.
If that caller's own seek is rejected, started_at (and, for a
was-paused _seek_deck call, paused_position) must be corrected back to
reflect the deck's real, unseeked position -- otherwise the dashboard,
crossfade timing, and pause/resume state all keep reporting a target
the deck never actually reached."""
import inspect
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import gi
from django.test import TransactionTestCase

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

import library.services.engine as eng_module
from library.services.engine import DECK_STUCK_TIMEOUT_SECONDS, SEEK_EOS_GUARD_SECONDS, Deck
from library.tests.test_engine_deck_lifecycle import (
    _make_log_item,
    _make_real_engine,
    _make_track,
    _wait_until,
    _write_wav,
)


def make_stand_in():
    """A bare PlaybackEngine instance, bypassing __init__ -- same
    technique as library/tests/test_hour_log_async_build.py's
    make_stand_in, safe for methods that only touch instance
    attributes and (mocked) deck/pipeline state."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.running = True
    obj.decks = {"A": None, "B": None}
    obj.manual_mode = False
    obj._lock = threading.RLock()
    obj._deck_bin_map = {}
    return obj


def make_deck(slot="A", duration_seconds=180.0, title="Test Track", seconds_since_seek=None):
    """seconds_since_seek=None means never seeked (Deck.seeked_at stays
    None, its real default); a numeric value backdates seeked_at that
    many real seconds into the past, so tests can exercise "just
    seeked" vs "seeked, but outside the guard window" without needing
    to mock time.time() itself."""
    track = MagicMock(id=1, title=title, duration_seconds=duration_seconds)
    deck = Deck(slot=slot, track=track, log_item=MagicMock(), pipeline=MagicMock(), mixer_pad=MagicMock())
    if seconds_since_seek is not None:
        deck.seeked_at = time.time() - seconds_since_seek
    return deck


class EOSPlausibilityTests(TransactionTestCase):
    def _probe(self, stand_in, deck, position):
        deck_bin = object()
        stand_in._deck_bin_map[id(deck_bin)] = deck
        stand_in._get_deck_position = MagicMock(return_value=position)
        stand_in._handle_deck_finished = MagicMock()
        eng_module.PlaybackEngine._on_deck_eos_probed(stand_in, deck_bin)
        return stand_in._handle_deck_finished

    # -- Seek-window scoping: the core of this design --

    def test_never_seeked_deck_eos_always_honored_regardless_of_position(self):
        """An unseeked deck's EOS must behave exactly as it always
        did -- position 2s into a 7194s track would be wildly
        "implausible" by position alone, but with no recent seek to
        justify suspicion, this must still be trusted."""
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=7194.0, seconds_since_seek=None)

        mock_finished = self._probe(stand_in, deck, position=2.0)

        mock_finished.assert_called_once_with(deck)

    def test_seek_outside_guard_window_eos_always_honored(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=7194.0, seconds_since_seek=SEEK_EOS_GUARD_SECONDS + 5.0)

        mock_finished = self._probe(stand_in, deck, position=2.0)

        mock_finished.assert_called_once_with(deck)

    def test_seek_just_inside_guard_window_implausible_eos_is_ignored(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=7194.0, seconds_since_seek=SEEK_EOS_GUARD_SECONDS - 0.1)

        mock_finished = self._probe(stand_in, deck, position=6446.6)

        mock_finished.assert_not_called()
        self.assertFalse(deck.finished, "the deck must be left alone, not marked finished")
        self.assertIn("I_EOS_REJECTED_POST_SEEK", deck.eos_milestones)
        self.assertNotIn("I_EOS_ACCEPTED", deck.eos_milestones)

    # -- Plausibility margin, within the seek window --

    def test_plausible_eos_near_true_end_is_honored(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=180.0, seconds_since_seek=0.1)

        mock_finished = self._probe(stand_in, deck, position=179.5)

        mock_finished.assert_called_once_with(deck)

    def test_eos_exactly_at_the_plausibility_margin_is_honored(self):
        # "<", not "<=" -- exactly at the boundary is still plausible.
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=180.0, seconds_since_seek=0.1)

        mock_finished = self._probe(stand_in, deck, position=180.0 - DECK_STUCK_TIMEOUT_SECONDS)

        mock_finished.assert_called_once_with(deck)

    def test_eos_one_second_past_the_margin_is_implausible(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=180.0, seconds_since_seek=0.1)

        mock_finished = self._probe(stand_in, deck, position=180.0 - DECK_STUCK_TIMEOUT_SECONDS - 1.0)

        mock_finished.assert_not_called()

    # -- The short-track bug from review round 1 --

    def test_short_track_implausible_eos_is_still_caught(self):
        """The bug in the first version of this fix: duration - 30
        went negative for anything under 30s, so the check silently
        never engaged. margin is now capped at duration/2 instead."""
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=20.0, seconds_since_seek=0.1)

        mock_finished = self._probe(stand_in, deck, position=2.0)

        mock_finished.assert_not_called()

    def test_short_track_eos_near_true_end_is_still_honored(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=20.0, seconds_since_seek=0.1)

        mock_finished = self._probe(stand_in, deck, position=19.5)

        mock_finished.assert_called_once_with(deck)

    def test_very_short_dedication_intro_length_track(self):
        """Dedication intros are ~5-8s -- duration/2 margin still
        behaves sensibly at this length."""
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=5.568, seconds_since_seek=0.1)

        mock_finished_implausible = self._probe(stand_in, deck, position=0.5)
        mock_finished_implausible.assert_not_called()

        deck2 = make_deck(duration_seconds=5.568, seconds_since_seek=0.1)
        mock_finished_plausible = self._probe(stand_in, deck2, position=5.4)
        mock_finished_plausible.assert_called_once_with(deck2)

    def test_missing_duration_falls_through_even_within_seek_window(self):
        """Can't evaluate plausibility without a known duration --
        matches _check_stuck_decks' own `if not duration: continue`
        guard, same reasoning."""
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=None, seconds_since_seek=0.1)

        mock_finished = self._probe(stand_in, deck, position=5.0)

        mock_finished.assert_called_once_with(deck)

    # -- Guards unrelated to the seek-window logic --

    def test_already_finished_deck_short_circuits_before_any_check(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=180.0, seconds_since_seek=0.1)
        deck.finished = True
        deck_bin = object()
        stand_in._deck_bin_map[id(deck_bin)] = deck
        stand_in._get_deck_position = MagicMock(return_value=999.0)
        stand_in._handle_deck_finished = MagicMock()

        eng_module.PlaybackEngine._on_deck_eos_probed(stand_in, deck_bin)

        stand_in._get_deck_position.assert_not_called()
        stand_in._handle_deck_finished.assert_not_called()

    def test_unknown_deck_bin_is_a_noop(self):
        stand_in = make_stand_in()
        stand_in._handle_deck_finished = MagicMock()

        eng_module.PlaybackEngine._on_deck_eos_probed(stand_in, object())

        stand_in._handle_deck_finished.assert_not_called()

    def test_implausible_eos_emits_a_monitoring_event(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=7194.0, seconds_since_seek=0.1)

        with patch.object(eng_module, "emit_event") as mock_emit:
            self._probe(stand_in, deck, position=6446.6)

        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["category"], "engine")
        self.assertEqual(mock_emit.call_args.kwargs["detail"]["track_id"], deck.track.id)
        self.assertIn("seconds_since_seek", mock_emit.call_args.kwargs["detail"])

    def test_error_path_still_tears_down_regardless_of_position(self):
        """_on_deck_error (a genuine GStreamer pipeline error, not an
        EOS) must NOT go through this plausibility gate -- a real
        pipeline error deserves a full teardown regardless of position;
        silently ignoring it would leave a broken deck producing no
        audio with no recovery path. Static check: _on_deck_error schedules
        the dedicated retire-and-record callback, not the position-gated
        EOS path."""
        src = inspect.getsource(eng_module.PlaybackEngine._on_deck_error)
        self.assertIn("_retire_deck_error_and_record", src)
        self.assertNotIn("_get_deck_position", src)


class SeekRejectionHandlingTests(TransactionTestCase):
    """seek_simple()'s boolean return value is checked at all three
    seek-application sites -- confirmed via an isolated repro that this
    does NOT catch the transient post-seek parser hiccup (seek_simple
    still returns True there), but it's real hygiene for the rarer,
    different case of a seek genuinely being rejected outright (wrong
    pipeline state, non-seekable source). The behavioral regressions below
    execute the real recreate/seek methods while substituting only the deck
    construction and GStreamer boundary calls, so bookkeeping, pad-offset
    rebasing, pause handling, monitoring, and logging remain observable."""

    def test_create_deck_checks_auto_resume_seek_result(self):
        src = inspect.getsource(eng_module.PlaybackEngine._create_deck)
        self.assertIn("seek_ok = deck.pipeline.seek_simple", src)
        self.assertIn("if not seek_ok:", src)
        self.assertIn("deck.seeked_at = time.time()", src)

    def test_rejected_seek_does_not_corrupt_started_at_with_unreached_target(self):
        """A rejected auto-resume seek must NOT overwrite started_at to
        claim the target position -- the deck is genuinely still at 0,
        and the earlier (already-correct) started_at assignment must
        survive untouched. Regression for a bug caught while writing
        this fix (not by the outside review): the first draft applied
        the started_at rewrite unconditionally regardless of seek_ok.
        _create_deck has several unrelated "except Exception as exc:"
        blocks elsewhere in the function, so anchor narrowly on the
        "if not seek_ok: ... else:" pair itself rather than searching
        for the next except clause (which could belong to a different,
        earlier try block)."""
        src = inspect.getsource(eng_module.PlaybackEngine._create_deck)
        start = src.index("if not seek_ok:")
        end = src.index("else:", start)
        rejection_branch = src[start:end]
        self.assertNotIn("deck.started_at = time.time() - (_auto_resume_position_ns", rejection_branch)
        self.assertIn("deck.seeked_at = time.time()", src[end:end + 200])


def _gated_seek_fixture(duration_seconds=6.0):
    """A real, hardware-free _seek_deck/_resume_deck fixture -- same
    _make_real_engine()/_write_wav() building blocks as
    test_engine_deck_lifecycle.py's RealDeckTopologyTests, long enough
    (default 6s) that a mid-file seek target is meaningful. Returns
    (engine, wav_path, track, log_item); the caller owns the
    TemporaryDirectory/engine cleanup."""
    temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-gated-seek.")
    wav_path = Path(temp_dir.name) / "seek-source.wav"
    _write_wav(wav_path, frames=int(duration_seconds * 44100))
    engine = _make_real_engine()
    track = _make_track(wav_path, track_id=1, duration=duration_seconds, title="Gated Seek Track")
    log_item = _make_log_item(track, item_id=1)
    return engine, temp_dir, track, log_item


def _pump_engine(engine, predicate, timeout=3.0):
    """Pumps the default GLib context (needed for streaming-thread-driven
    probe callbacks and the GLib.idle_add EOS handoff to run at all) AND
    drives _deck_seek_tick on every pass -- rather than registering the
    tick via GLib.timeout_add, which would tie the test to a real
    recurring wall-clock timer it has no reason to depend on. A harmless
    no-op call when no deck has a gated seek in flight, so this replaces
    plain _wait_until throughout this module's gated-seek tests.

    Also explicitly drains engine.main_pipeline's bus on every pass.
    Empirically (see the r0063 report), a second/third decodebin's own
    internal ASYNC PAUSED->PLAYING completion can sit unresolved
    indefinitely under a bare add_signal_watch()-plus-context.iteration()
    pump in a tight, no-real-blocking test loop -- draining the bus
    directly is what actually unsticks it. Production is unaffected:
    the real engine drives a genuine GLib.MainLoop.run(), which dispatches
    the bus exactly as intended; this is a test-harness-only pumping
    subtlety, not a behavior this fix depends on."""
    context = GLib.MainContext.default()
    bus = engine.main_pipeline.get_bus()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        while bus.pop() is not None:
            pass
        engine._deck_seek_tick()
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def _settle_teardowns_then_stop(engine, timeout=10.0):
    """addCleanup helper for every gated-seek test class below, used in
    place of calling .stop() on each per-slot BoundedTeardownCoordinator
    directly. .stop() only asks an idle worker to exit -- per its own
    docstring, it "deliberately never joins a hung one" -- and, more
    importantly, a worker with a still-nonempty queue keeps draining
    it even after _stopping is set, rather than abandoning the backlog.
    A test that dispatches many deck removals without individually
    waiting on each one's real NULL transition (repeated-cycle and
    stress tests especially) can therefore leave a same-named
    "deck-real-<slot>-test-worker" thread genuinely alive and still
    working for a little while after the test method itself returns --
    long enough, in practice, to confuse an unrelated LATER test's own
    threading.enumerate()-based assertions about ITS OWN identically-
    named worker (test_engine_deck_lifecycle.py's _make_real_engine()
    fixture hardcodes that name per slot, not per test instance).
    Waiting here for each coordinator to have fully drained its queue
    BEFORE calling .stop() means no test in this file ever hands off a
    backlog to whatever runs next."""
    for slot in ("A", "B"):
        coordinator = engine._deck_teardowns.get(slot)
        if coordinator is None:
            continue

        def _settled(coordinator=coordinator):
            snap = coordinator.snapshot()
            return snap["queue_depth"] == 0 and snap["active_generation"] is None

        _pump_engine(engine, _settled, timeout=timeout)
        coordinator.stop()


def _pump_until_seek_resolved(engine, deck, timeout=3.0):
    return _pump_engine(engine, lambda: deck.gated_seek is None, timeout=timeout)


def _pump_until_seeking(engine, deck, timeout=3.0):
    return _pump_engine(
        engine,
        lambda: deck.gated_seek is not None and deck.gated_seek["phase"] == "seeking",
        timeout=timeout,
    )


def _controllable_seek(hold_event, real_seek_simple):
    """A seek_simple() replacement that parks the calling thread (the
    real background worker _dispatch_gated_seek_call starts) on
    hold_event until the test releases it, THEN performs the real,
    original seek_simple() call underneath -- so a released hold still
    exercises a genuine flush/position change, not a fake success. Lets
    a lifecycle-collision test land its action while the native call is
    DETERMINISTICALLY, verifiably still "in flight" (phase ==
    "seeking"), without needing an actual GStreamer-level hang -- the
    seam the r0063 ownership-invariant hardening report's stress harness
    needs."""

    def _fn(*args, **kwargs):
        hold_event.wait()
        return real_seek_simple(*args, **kwargs)

    return _fn


class GatedSeekSuccessTests(TransactionTestCase):
    """_begin_gated_seek (shared by _seek_deck/_resume_deck) gates a
    freshly linked replacement deck behind a BLOCK_DOWNSTREAM probe on
    its own ghost pad -- topologically connected to the live mixer
    exactly as before, but never exchanging a single real buffer with it
    -- until a flushing seek to the requested position is confirmed. See
    the r0063 architecture report for the 290-cycle hardware-free harness
    this design is based on: the immediate-seek-after-link approach it
    replaces rejected a freshly linked bin's own flushing seek in 29/30
    cycles of one representative run; gating removes that race (0
    rejections / 0 hangs / exact position accuracy across 290 cycles)."""

    def setUp(self):
        self.engine, self.temp_dir, self.track, self.log_item = _gated_seek_fixture()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(lambda: self.engine.main_pipeline.set_state(Gst.State.NULL))
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

    def test_successful_manual_seek_lands_on_target_with_no_position_zero_leakage(self):
        self.engine._create_deck("A", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["A"].media_buffer_count > 0))

        self.engine._seek_deck("A", 3.0)
        deck = self.engine.decks["A"]
        self.assertIsNotNone(deck.gated_seek)
        # Still fully gated immediately after the request -- nothing has
        # been exposed to the mixer yet, matching "no position-zero
        # leakage" even for the transient moment before confirmation.
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 1)

        self.assertTrue(_pump_until_seek_resolved(self.engine, deck))
        self.assertIsNone(deck.gated_seek)
        self.assertIsNotNone(deck.seeked_at)
        ok, pos = deck.pipeline.query_position(Gst.Format.TIME)
        self.assertTrue(ok)
        self.assertAlmostEqual(pos / Gst.SECOND, 3.0, delta=0.05)

    def test_repeated_seek_preparation_cycles_leave_no_stale_pads_or_map_entries(self):
        self.engine._create_deck("A", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["A"].media_buffer_count > 0))

        for target in (1.0, 2.0, 3.0, 1.5, 2.5):
            self.engine._seek_deck("A", target)
            deck = self.engine.decks["A"]
            self.assertTrue(_pump_until_seek_resolved(self.engine, deck))

        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 1)
        self.assertEqual(len(self.engine._deck_bin_map), 1)
        survivor = self.engine.decks["A"]
        self.assertIsNotNone(survivor)
        self.assertFalse(survivor.finished)
        self.assertGreater(len(survivor.probe_handles), 0)

    def test_pause_then_resume_shares_the_same_gated_primitive(self):
        self.engine._create_deck("A", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["A"].media_buffer_count > 0))

        self.engine._pause_deck("A")
        self.assertTrue(self.engine.decks["A"].paused)

        self.engine._resume_deck("A")
        deck = self.engine.decks["A"]
        self.assertIsNotNone(deck.gated_seek)
        self.assertTrue(_pump_until_seek_resolved(self.engine, deck))
        self.assertFalse(deck.paused)
        self.assertIsNotNone(deck.seeked_at)

    def test_second_request_while_one_is_in_flight_is_dropped_not_applied(self):
        """Empirically the whole prepare-through-confirm sequence
        resolves in well under a millisecond once gated (see the
        report), so a real second request landing inside that window is
        not expected in practice -- this proves the drop path itself is
        safe (no exception, no corrupted state, the in-flight seek still
        resolves normally) rather than asserting a specific coalescing
        outcome."""
        self.engine._create_deck("A", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["A"].media_buffer_count > 0))

        self.engine._seek_deck("A", 3.0)
        deck = self.engine.decks["A"]
        first_op = deck.gated_seek
        self.assertIsNotNone(first_op)

        with patch.object(eng_module, "emit_event") as mock_emit:
            self.engine._seek_deck("A", 4.0)

        # Untouched -- the second request was dropped, not applied.
        self.assertIs(deck.gated_seek, first_op)
        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["title"], "Seek request dropped (already in progress)")

        self.assertTrue(_pump_until_seek_resolved(self.engine, deck))
        ok, pos = deck.pipeline.query_position(Gst.Format.TIME)
        self.assertTrue(ok)
        self.assertAlmostEqual(pos / Gst.SECOND, 3.0, delta=0.05)


class GatedSeekRejectionTests(TransactionTestCase):
    """seek_simple()'s boolean return value is checked exactly once,
    inside the background worker _dispatch_gated_seek_call dispatches --
    a real, hardware-free rejection is forced by overriding the
    instance's own seek_simple after the gate has armed (PyGObject
    instances accept plain Python attribute overrides), proving the same
    r0062 fallback correctness (pad-offset rebased to the CONFIRMED
    running time, not the unreached target; started_at/paused_position
    describe the real position) now runs from _resolve_gated_seek."""

    def setUp(self):
        self.engine, self.temp_dir, self.track, self.log_item = _gated_seek_fixture()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(lambda: self.engine.main_pipeline.set_state(Gst.State.NULL))
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

    def test_manual_seek_rejection_rebases_pad_timeline_to_actual_zero(self):
        self.engine._create_deck("A", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["A"].media_buffer_count > 0))

        self.engine._seek_deck("A", 3.0)
        deck = self.engine.decks["A"]
        # Overridden immediately -- _dispatch_gated_seek_call fires the
        # instant the block probe's first hit is observed, which can
        # itself happen before a test-side wait for "phase == seeking"
        # ever gets scheduled; only a same-statement-window override is
        # race-free against the background worker's own dispatch.
        deck.pipeline.seek_simple = lambda *a, **kw: False

        with patch.object(eng_module, "emit_event") as mock_emit, patch("builtins.print") as mock_print:
            self.assertTrue(_pump_until_seek_resolved(self.engine, deck))

        self.assertIsNone(deck.gated_seek)
        self.assertAlmostEqual(deck.started_at, time.time(), delta=0.5)
        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["detail"]["fallback_seconds"], 0.0)
        self.assertTrue(mock_emit.call_args.kwargs["detail"]["pad_offset_rebased"])
        messages = [call.args[0] for call in mock_print.call_args_list if call.args]
        self.assertTrue(any("rejected -- playing from 0 instead" in item for item in messages))
        self.assertFalse(any("Seek to 3.0s" in item for item in messages if "rejected" not in item))

    def test_resume_seek_rejection_rebases_pad_timeline_and_preserves_pause_derived_position(self):
        # resume_position_ns=0 -- a genuine fresh (unseeked) deck, so its
        # real internal position actually starts at (and briefly after
        # creation, sits near) zero, unlike resume_position_ns=<target>
        # which is only ever a pre-seek ASSUMPTION until a real seek
        # confirms it (the exact hazard r0062 already guards against
        # elsewhere) -- irrelevant for this test anyway, since only
        # _resume_deck's REJECTION behavior is under test here, not the
        # specific pre-pause position.
        self.engine._create_deck("A", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["A"].media_buffer_count > 0))
        # A real pause (not hand-set flags) so the deck is genuinely
        # unlinked from the mixer beforehand, exactly as _resume_deck
        # would actually encounter it in production.
        self.engine._pause_deck("A")

        self.engine._resume_deck("A")
        deck = self.engine.decks["A"]
        deck.pipeline.seek_simple = lambda *a, **kw: False

        with patch.object(eng_module, "emit_event") as mock_emit, patch("builtins.print"):
            self.assertTrue(_pump_until_seek_resolved(self.engine, deck))

        self.assertIsNone(deck.gated_seek)
        self.assertEqual(mock_emit.call_args.kwargs["detail"]["fallback_seconds"], 0.0)
        # was_paused=False for _resume_deck (its whole point is coming
        # OUT of pause) -- rejection must not re-pause the deck.
        self.assertFalse(deck.paused)

    def test_seek_deck_does_not_clobber_pause_derived_position_on_rejected_seek(self):
        """The was_paused branch must not overwrite _pause_deck's own
        freshly derived paused_position with the unreached seek target
        when the seek was rejected."""
        self.engine._create_deck("A", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["A"].media_buffer_count > 0))
        self.engine._pause_deck("A")

        self.engine._seek_deck("A", 5.0)
        deck = self.engine.decks["A"]
        deck.pipeline.seek_simple = lambda *a, **kw: False

        with patch.object(eng_module, "emit_event"), patch("builtins.print"):
            self.assertTrue(_pump_until_seek_resolved(self.engine, deck))

        self.assertTrue(deck.paused)
        self.assertNotEqual(deck.paused_position, 5.0)
        self.assertLess(deck.paused_position, 1.0)


class GatedSeekAbandonmentTests(TransactionTestCase):
    """If the native seek call itself never returns, the generation must
    be permanently isolated (never unblocked, never touched again) and
    the slot must recover with a fresh position-0 replacement -- bounded
    failure, never an unbounded hang, mirroring
    BoundedTeardownCoordinator's own poison-on-timeout posture for an
    unrecoverable set_state(NULL). The stuck call here is a plain daemon
    thread parked in time.sleep(); Python threads can't be killed, but
    daemon threads never block interpreter/process exit, so this is safe
    to exercise directly in-process (unlike the investigation harness's
    subprocess-per-cycle discipline, which exists for genuinely
    unkillable NATIVE calls)."""

    def setUp(self):
        self.engine, self.temp_dir, self.track, self.log_item = _gated_seek_fixture()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(lambda: self.engine.main_pipeline.set_state(Gst.State.NULL))
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

    def test_stuck_seek_call_is_abandoned_and_slot_recovers_at_zero(self):
        self.engine._create_deck("A", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["A"].media_buffer_count > 0))

        self.engine._seek_deck("A", 3.0)
        stuck_deck = self.engine.decks["A"]
        # Overridden immediately, before any pumping -- _dispatch_gated_
        # seek_call fires the instant the block probe's first hit is
        # observed, which can happen inside the tick call above before a
        # test-side wait for a later phase would ever get scheduled;
        # only a same-statement-window override is race-free.
        stuck_deck.pipeline.seek_simple = lambda *a, **kw: (time.sleep(999), True)[1]

        with (
            patch.object(eng_module, "DECK_SEEK_CALL_TIMEOUT_SECONDS", 0.2),
            patch.object(eng_module, "emit_event") as mock_emit,
            patch("builtins.print"),
        ):
            # _advance_gated_seek re-reads DECK_SEEK_CALL_TIMEOUT_SECONDS
            # from the module namespace on every tick, so patching it
            # here (rather than before _seek_deck) still applies in time
            # for the abandonment check.
            self.assertTrue(
                _pump_engine(self.engine, lambda: self.engine.decks["A"] is not stuck_deck, timeout=3.0)
            )

        self.assertTrue(stuck_deck.finished)
        self.assertTrue(stuck_deck.retirement_started)
        self.assertIsNone(stuck_deck.gated_seek)
        self.assertNotIn(id(stuck_deck.pipeline), self.engine._deck_bin_map)
        replacement = self.engine.decks["A"]
        self.assertIsNotNone(replacement)
        self.assertIsNot(replacement, stuck_deck)
        self.assertEqual(mock_emit.call_args_list[0].kwargs["level"], "critical")
        self.assertTrue(mock_emit.call_args_list[0].kwargs["detail"]["restart_recommended"])
        # r0063 ownership-invariant hardening: the abandoned generation's
        # mixer request pad is deliberately never released (touching it
        # again risks the exact contention that just wedged it) -- that
        # leak must be explicitly counted, not silent, and the mixer
        # should show exactly one leaked pad plus the replacement's own
        # fresh one.
        self.assertEqual(self.engine._quarantined_seek_generation_count(), 1)
        self.assertTrue(mock_emit.call_args_list[0].kwargs["detail"]["mixer_request_pad_leaked"])
        self.assertEqual(
            mock_emit.call_args_list[0].kwargs["detail"]["cumulative_quarantined_generations"], 1
        )
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 2)


class NeverPrerolledTests(TransactionTestCase):
    """_advance_gated_seek's "prerolling" phase: the gate condition
    (a first BLOCK_DOWNSTREAM hit) may simply never arrive -- a source
    that never produces any data at all, real-world analog being e.g. a
    file on a wedged network mount. Forced deterministically here with a
    named pipe (FIFO) as the track's own file, with a background thread
    holding the WRITE end permanently open but never writing a byte to
    it: filesrc's open() (synchronous, part of the fast NULL->READY
    state change) completes immediately since a writer is already
    present, but every subsequent read() then blocks forever on
    decodebin's own internal streaming thread -- typefind/decodebin
    genuinely never receive a single byte, so no sticky event, no
    buffer, nothing ever reaches the gate. (A bare FIFO with no writer
    at all was tried first and rejected: filesrc's open() itself then
    blocks -- on the calling GLib/test thread, synchronously, as part of
    _create_deck -- which would hang this test's own process, not just
    the deck being tested.)"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-never-prerolled.")
        self.addCleanup(self.temp_dir.cleanup)
        self.fifo_path = Path(self.temp_dir.name) / "never-arrives.wav"
        os.mkfifo(self.fifo_path)
        # os.open(..., O_WRONLY) on a FIFO blocks until a reader opens
        # it too -- filesrc (the reader) doesn't attempt that until the
        # test method actually triggers deck creation, so this thread is
        # started here but not waited on until then. Once both ends are
        # open, this thread just sits there forever -- daemon, never
        # joined, the fd intentionally never closed or written to for
        # the rest of the test.
        self._fifo_writer_fd = None
        self._fifo_writer_opened = threading.Event()

        def _hold_fifo_writer_open():
            self._fifo_writer_fd = os.open(str(self.fifo_path), os.O_WRONLY)
            self._fifo_writer_opened.set()

        threading.Thread(target=_hold_fifo_writer_open, daemon=True).start()

        self.engine = _make_real_engine()
        # Deliberately NOT a bare main_pipeline.set_state(NULL) -- see
        # _detach_never_nulled_stuck_deck's own docstring. Registered
        # BEFORE that NULL cleanup so it runs AFTER it (addCleanup is
        # LIFO): detach the still-abandoned FIFO deck first, then NULL
        # the (now child-free-of-it) pipeline.
        self.addCleanup(lambda: self.engine.main_pipeline.set_state(Gst.State.NULL))
        self.addCleanup(lambda: self._detach_never_nulled_stuck_deck("A"))
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))
        self.track = _make_track(self.fifo_path, track_id=1, duration=6.0, title="Never Prerolls")
        self.log_item = _make_log_item(self.track, item_id=1)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

    def _detach_never_nulled_stuck_deck(self, slot):
        """This test class deliberately leaves ITS OWN never-prerolled,
        FIFO-blocked deck in place rather than ever calling _remove_deck
        on it (see the test method's own comment on why that specific
        removal is unsafe). But main_pipeline.set_state(Gst.State.NULL)
        -- an ordinary, otherwise-harmless cleanup step every other test
        class in this file uses -- would ITSELF then hang forever
        transitioning that same stuck child bin to NULL as part of
        nulling the whole pipeline. Confirmed directly: this exact
        sequence (leave deck A in place, then main_pipeline.set_state
        (NULL)) hangs the test process, not just the deck.
        Unlink+remove (proven fast/safe elsewhere in this file for
        exactly this reason) detaches it from main_pipeline WITHOUT
        ever calling set_state on it, so the pipeline's own NULL
        transition no longer has to wait on it -- the bin itself is
        simply leaked (never reaches NULL, matching this whole design's
        established "abandon it, never touch it again" posture for a
        generation something might still be blocked inside)."""
        deck = self.engine.decks.get(slot)
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

    def test_never_prerolled_resolves_boundedly_with_truthful_zero_position(self):
        with (
            patch.object(eng_module, "DECK_SEEK_PREROLL_TIMEOUT_SECONDS", 0.3),
            # _log_item_playable's Path(fp).is_file() check rejects a
            # FIFO outright (it isn't a regular file) -- bypassed here so
            # _create_deck reaches real GStreamer construction at all;
            # the FIFO itself is what then keeps decode from ever
            # producing anything, which is the actual thing under test.
            patch.object(eng_module, "_log_item_playable", return_value=(True, None)),
        ):
            # _begin_gated_seek directly, not _seek_deck -- _seek_deck
            # requires an already-occupied slot to seek FROM (it reads
            # log_item off the existing deck), which is irrelevant here:
            # this test wants a freshly gated deck targeting the FIFO
            # from an empty slot, exactly the shared primitive
            # _seek_deck/_resume_deck both call into regardless.
            self.engine._begin_gated_seek("A", self.log_item, 3.0, was_paused=False)
            deck = self.engine.decks["A"]
            self.assertIsNotNone(deck)
            self.assertIsNotNone(deck.gated_seek)
            # Confirms the fixture itself actually connected (filesrc's
            # read-side open unblocked the writer thread's open too) --
            # if this is ever false the test below would otherwise be
            # trivially/vacuously true for the wrong reason.
            self.assertTrue(self._fifo_writer_opened.wait(timeout=2.0))

            with patch.object(eng_module, "emit_event") as mock_emit, patch("builtins.print"):
                resolved = _pump_until_seek_resolved(self.engine, deck, timeout=3.0)

        # Bounded resolution -- no hang, no stale gated-seek record.
        self.assertTrue(resolved)
        self.assertIsNone(deck.gated_seek)
        # No false seek success: rejected, not accepted -- the file
        # never even opened, so decode genuinely never started.
        self.assertEqual(mock_emit.call_args.kwargs["title"], "Deck seek rejected")
        self.assertEqual(mock_emit.call_args.kwargs["detail"]["fallback_seconds"], 0.0)
        # Truthful playback/timing state: started_at reflects "now",
        # not the never-reached target.
        self.assertAlmostEqual(deck.started_at, time.time(), delta=0.5)
        self.assertIsNone(deck.seeked_at)
        # Deliberately NOT calling _remove_deck(deck) here. A "never
        # prerolled" outcome only tells us the GATE condition never
        # arrived within DECK_SEEK_PREROLL_TIMEOUT_SECONDS -- it says
        # nothing about WHY. In this test the reason is a permanently
        # blocked filesrc read(), and _remove_deck's deferred
        # set_state(NULL) on a bin whose filesrc is stuck in a blocking
        # read can hang the SAME way an unresolved native seek call can
        # -- confirmed empirically (attempting it here left a genuinely
        # permanent "deck-real-a-test-worker" thread, since nothing ever
        # closes this test's own FIFO writer). That specific hazard --
        # a hung underlying source, as opposed to a hung native seek
        # call -- is pre-existing and orthogonal to gated seeking
        # (equally present for a perfectly ordinary, non-seek fresh
        # track start against the same kind of hung source) and out of
        # scope for this hardening pass; noted for the report rather
        # than fixed here. What this test verifies is the rejected-seek
        # fallback itself (already asserted above): truthful position,
        # no false success, no stale gated-seek record -- and that a
        # SEPARATE slot remains completely unaffected by this one
        # deliberately-abandoned generation.
        real_wav = Path(self.temp_dir.name) / "real.wav"
        _write_wav(real_wav, frames=6 * 44100)
        real_track = _make_track(real_wav, track_id=2, duration=6.0, title="Real Track")
        real_log_item = _make_log_item(real_track, item_id=2)
        self.engine._create_deck("B", real_log_item, resume_position_ns=0)
        self.assertIsNotNone(self.engine.decks["B"])
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["B"].media_buffer_count > 0))
        self.addCleanup(lambda: self.engine._remove_deck(self.engine.decks["B"]))


class AcceptedUnconfirmedTests(TransactionTestCase):
    """_advance_gated_seek's "confirming" phase: seek_simple() itself
    returns True (the native call is no longer executing, so nothing
    here risks racing it), but no fresh buffer ever reaches the gate to
    POSITIVELY confirm decode actually resumed at the claimed position.
    Forced deterministically by overriding seek_simple with a pure no-op
    that lies "True" without performing any real seek at all -- nothing
    flushes the block probe's already-held first hit, so no second hit
    can ever arrive."""

    def setUp(self):
        self.engine, self.temp_dir, self.track, self.log_item = _gated_seek_fixture()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(lambda: self.engine.main_pipeline.set_state(Gst.State.NULL))
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

    def test_accepted_unconfirmed_is_never_reported_as_successful(self):
        self.engine._create_deck("A", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["A"].media_buffer_count > 0))

        self.engine._seek_deck("A", 3.0)
        deck = self.engine.decks["A"]
        # Overridden immediately -- see the same race note in
        # GatedSeekRejectionTests: _dispatch_gated_seek_call fires the
        # instant the block probe's first hit is observed, which can
        # happen before a test-side wait for a later phase would ever
        # get scheduled.
        deck.pipeline.seek_simple = lambda *a, **kw: True  # accepts, but performs no real seek

        with (
            patch.object(eng_module, "DECK_SEEK_POST_SEEK_CONFIRM_TIMEOUT_SECONDS", 0.3),
            patch.object(eng_module, "emit_event") as mock_emit,
            patch("builtins.print"),
        ):
            resolved = _pump_until_seek_resolved(self.engine, deck, timeout=3.0)

        # Bounded timeout -- no hang, no stale gated-seek record.
        self.assertTrue(resolved)
        self.assertIsNone(deck.gated_seek)
        # No target falsely reported as reached.
        self.assertEqual(mock_emit.call_args.kwargs["title"], "Deck seek unconfirmed -- replaced at zero")
        self.assertEqual(mock_emit.call_args.kwargs["detail"]["target_seconds"], 3.0)
        self.assertEqual(mock_emit.call_args.kwargs["detail"]["fallback_seconds"], 0.0)
        # Safe fallback semantics -- the original (unconfirmed) generation
        # is genuinely retired (not left playing, not left leaked), and a
        # fresh, honest replacement takes over the slot, at position 0
        # (no stale target-based pad offset survives onto it).
        self.assertTrue(deck.finished)
        self.assertTrue(deck.retirement_started)
        replacement = self.engine.decks["A"]
        self.assertIsNotNone(replacement)
        self.assertIsNot(replacement, deck)
        self.assertIsNone(replacement.seeked_at)
        self.assertAlmostEqual(replacement.started_at, time.time(), delta=0.5)
        # No leaked pad here -- unlike timeout_abandon, the native call
        # already returned, so full, ordinary cleanup was safe.
        self.assertEqual(self.engine._quarantined_seek_generation_count(), 0)
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 1)
        self.assertEqual(len(self.engine._deck_bin_map), 1)
        # Engine continues -- the replacement itself is a completely
        # normal, healthy deck.
        self.assertTrue(_pump_engine(self.engine, lambda: replacement.media_buffer_count > 0))


class GatedSeekLifecycleCollisionTests(TransactionTestCase):
    """The ownership invariant this whole hardening pass exists for:
    *no thread may destructively mutate or retire a Gst.Bin while an
    unresolved native seek call can still be operating on that same
    bin*. Exercised with _controllable_seek, which parks the REAL
    background worker _dispatch_gated_seek_call starts on a
    threading.Event -- phase == "seeking" here means a native call is
    GENUINELY, verifiably still executing, not merely "recently
    dispatched"."""

    def setUp(self):
        self.engine, self.temp_dir, self.track, self.log_item = _gated_seek_fixture()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(lambda: self.engine.main_pipeline.set_state(Gst.State.NULL))
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

    def _begin_inflight_seek(self, slot="A", target=3.0):
        """Common setup for every scenario below: a real deck, seeked,
        with its native call genuinely parked mid-flight. Returns
        (deck, hold_event) -- the caller performs its collision action,
        then must hold.set() to let the worker return before the test
        ends (an unreleased Event just leaves one harmless permanently-
        blocked daemon thread, matching GatedSeekAbandonmentTests'
        module docstring, but releasing it keeps assertions about
        eventual resolution meaningful)."""
        self.engine._create_deck(slot, self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks[slot].media_buffer_count > 0))
        self.engine._seek_deck(slot, target)
        deck = self.engine.decks[slot]
        hold = threading.Event()
        real_seek_simple = deck.pipeline.seek_simple
        deck.pipeline.seek_simple = _controllable_seek(hold, real_seek_simple)
        self.assertTrue(_pump_until_seeking(self.engine, deck))
        return deck, hold

    # -- A. Seek vs eject --

    def test_eject_during_inflight_native_seek_is_deferred_not_destructive(self):
        deck, hold = self._begin_inflight_seek()
        mixer_pad_before = deck.mixer_pad
        self.assertIsNotNone(mixer_pad_before)

        self.engine._eject_deck("A")

        # The native call may still be executing -- nothing about this
        # generation may be destructively touched yet.
        self.assertFalse(deck.finished)
        self.assertFalse(deck.retirement_started)
        self.assertIs(deck.mixer_pad, mixer_pad_before)
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 1)
        self.assertIn(id(deck.pipeline), self.engine._deck_bin_map)
        # Still "occupying" its slot from the engine's own bookkeeping
        # perspective -- eject hasn't (yet) actually happened.
        self.assertIs(self.engine.decks["A"], deck)

        hold.set()  # let the native call return

        self.assertTrue(_pump_engine(self.engine, lambda: deck.finished))
        self.assertTrue(deck.retirement_started)
        self.assertIsNone(deck.gated_seek)
        self.assertNotIn(id(deck.pipeline), self.engine._deck_bin_map)
        # _eject_deck's own _finish() (print + _start_next_track) ran
        # only once retirement actually completed; _start_next_track is
        # a no-op in this fixture, so the slot is simply empty now.
        self.assertIsNone(self.engine.decks["A"])
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)
        self.assertEqual(self.engine._quarantined_seek_generation_count(), 0)

    # -- B. Seek vs replacement/crossfade --

    def test_start_next_track_never_targets_a_slot_with_an_inflight_seek(self):
        """_start_next_track's own (pre-existing, unmodified) slot-
        occupancy guard -- `self.decks.get(slot) is not None` redirects
        to _free_slot() -- already makes generation N+1 creation on the
        SAME slot as an in-flight generation N structurally impossible:
        _create_deck is never even called for that slot while it's
        occupied, gated seek or not. Proven here against the REAL
        (unmocked) _start_next_track, with _next_queue_item and
        _create_deck stubbed only to keep the rest of the real method's
        body (DB-backed request-scheduling, dedication splicing -- both
        unrelated to the guard under test) from ever running: is_forced
        =True and category_id=None on the stub log_item make the real
        method skip straight from the guard to the _create_deck call
        this test inspects."""
        deck, hold = self._begin_inflight_seek()

        with (
            patch.object(self.engine, "_next_queue_item", return_value=(self.log_item, True)),
            patch.object(self.engine, "_create_deck") as mock_create_deck,
        ):
            eng_module.PlaybackEngine._start_next_track(self.engine, slot="A")

        # _create_deck was called (proving the guard didn't just bail
        # out entirely) but never against slot "A" -- it redirected to
        # the free slot "B" instead.
        mock_create_deck.assert_called_once()
        self.assertEqual(mock_create_deck.call_args.args[0], "B")

        # Slot A itself, and its genuinely in-flight generation, are
        # completely untouched.
        self.assertIs(self.engine.decks["A"], deck)
        self.assertFalse(deck.finished)
        self.assertIsNotNone(deck.gated_seek)
        self.assertEqual(deck.gated_seek["phase"], "seeking")

        hold.set()
        self.assertTrue(_pump_until_seek_resolved(self.engine, deck))

    # -- C. Seek vs second seek --

    def test_second_seek_during_genuinely_inflight_first_is_dropped(self):
        deck, hold = self._begin_inflight_seek(target=3.0)
        first_op = deck.gated_seek

        with patch.object(eng_module, "emit_event") as mock_emit:
            self.engine._seek_deck("A", 4.0)

        # Dropped, not applied or queued -- the exact same op object,
        # completely unmodified, still the one and only in-flight
        # operation.
        self.assertIs(deck.gated_seek, first_op)
        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["title"], "Seek request dropped (already in progress)")

        hold.set()
        self.assertTrue(_pump_until_seek_resolved(self.engine, deck))
        # The FIRST seek's target is what's actually reached -- the
        # dropped second request never influenced anything.
        ok, pos = deck.pipeline.query_position(Gst.Format.TIME)
        self.assertTrue(ok)
        self.assertAlmostEqual(pos / Gst.SECOND, 3.0, delta=0.05)

    def test_pause_during_genuinely_inflight_seek_is_dropped(self):
        deck, hold = self._begin_inflight_seek()
        with patch.object(eng_module, "emit_event") as mock_emit:
            self.engine._pause_deck("A")

        self.assertFalse(deck.paused)
        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["title"], "Pause request dropped (seek in flight)")

        hold.set()
        self.assertTrue(_pump_until_seek_resolved(self.engine, deck))

    # -- D. Seek vs engine shutdown --

    def test_shutdown_leaves_inflight_generation_untouched_and_remains_bounded(self):
        deck, hold = self._begin_inflight_seek()
        mixer_pad_before = deck.mixer_pad

        # A second, ordinary (not in flight) deck on slot B, to prove
        # shutdown still retires everything it safely can.
        self.engine._create_deck("B", self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks["B"].media_buffer_count > 0))
        healthy_deck = self.engine.decks["B"]

        start = time.monotonic()
        with patch.object(eng_module, "emit_event") as mock_emit:
            self.engine.stop()
        elapsed = time.monotonic() - start

        # Bounded -- stop() must not wait for the wedged native call.
        self.assertLess(elapsed, 2.0)
        # The in-flight generation is left completely untouched: never
        # unlinked, never NULL'd, its mixer request pad never released.
        self.assertFalse(deck.finished)
        self.assertFalse(deck.retirement_started)
        self.assertIs(deck.mixer_pad, mixer_pad_before)
        self.assertIn(id(deck.pipeline), self.engine._deck_bin_map)
        self.assertEqual(self.engine._quarantined_seek_generation_count(), 1)
        self.assertTrue(
            any(
                call.kwargs.get("title") == "Deck left untouched at shutdown (seek in flight)"
                for call in mock_emit.call_args_list
            )
        )
        # The healthy, not-in-flight deck on the other slot WAS retired
        # normally.
        self.assertTrue(healthy_deck.retirement_started)
        self.assertNotIn(id(healthy_deck.pipeline), self.engine._deck_bin_map)

        # Deliberately NOT releasing `hold` here (unlike every other
        # scenario in this class): main_pipeline is already NULL by this
        # point (stop()'s own tail), and letting the real, wrapped
        # seek_simple() run against a bin whose parent pipeline just
        # changed state out from under it -- purely a test-process
        # artifact, since production never touches these objects again
        # after stop() either -- is a needless risk to this test run's
        # own stability for no assertion this test still needs. One
        # permanently-parked daemon thread is the same accepted cost as
        # GatedSeekAbandonmentTests' own stuck-call scenario.


class MixedLifecycleStressTests(TransactionTestCase):
    """Repeated MIXED-operation cycles (not seek-only) against one
    persistent engine/pipeline -- per the r0063 hardening report's
    explicit request that normal-operation stress after the collision
    hardening vary seek success, eject/recreate, pause/resume, rapid
    back-to-back seeks, and ordinary remove/recreate, rather than only
    ever repeating the single seek-cycle shape GatedSeekSuccessTests
    already covers. No cumulative resource growth or ownership
    ambiguity is acceptable across the whole run."""

    def setUp(self):
        self.engine, self.temp_dir, self.track, self.log_item = _gated_seek_fixture(duration_seconds=10.0)
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(lambda: self.engine.main_pipeline.set_state(Gst.State.NULL))
        self.addCleanup(lambda: _settle_teardowns_then_stop(self.engine))
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)

    def _snapshot(self):
        return {
            "mixer_sinkpads": len(tuple(self.engine.mixer.sinkpads)),
            "deck_bin_map": len(self.engine._deck_bin_map),
            "decks_occupied": sum(1 for d in self.engine.decks.values() if d is not None),
            "quarantined": self.engine._quarantined_seek_generation_count(),
        }

    def test_100_mixed_lifecycle_cycles_show_no_cumulative_growth(self):
        slot = "A"
        self.engine._create_deck(slot, self.log_item, resume_position_ns=0)
        self.assertTrue(_pump_engine(self.engine, lambda: self.engine.decks[slot].media_buffer_count > 0))

        operations = [
            "seek", "seek", "eject_recreate", "seek", "pause_resume",
            "rapid_seek", "remove_recreate", "seek",
        ]
        num_cycles = 104  # >= 100, an even number of full passes over the pattern

        with patch.object(eng_module, "emit_event"), patch("builtins.print"):
            for i in range(num_cycles):
                op = operations[i % len(operations)]
                # Kept well clear of both ends of the 10s track -- not
                # this test's concern, but landing within
                # SEEK_EOS_GUARD's plausibility margin of the real end
                # would exercise a different, already-covered code path
                # instead of the lifecycle operation actually under test.
                target = 1.0 + (i % 4)

                # Every real engine command path that can empty a slot
                # (_eject_deck, natural EOS via _handle_deck_finished)
                # normally has _start_next_track refill it immediately
                # afterward -- mocked to a no-op in this fixture (deck
                # lifecycle, not queue/scheduling, is what's under test
                # here). Proactively refilling here keeps each cycle
                # starting from the same known-populated state a real
                # engine would actually have, rather than each
                # operation needing to defensively handle "slot
                # unexpectedly empty" as a special case of its own.
                if self.engine.decks.get(slot) is None:
                    self.engine._create_deck(slot, self.log_item, resume_position_ns=0)
                    self.assertTrue(
                        _pump_engine(
                            self.engine,
                            lambda: self.engine.decks[slot].media_buffer_count > 0,
                            timeout=3.0,
                        ),
                        f"cycle {i} ({op}): could not refill empty slot: {self._snapshot()}",
                    )

                if op in ("seek", "rapid_seek"):
                    self.engine._seek_deck(slot, target)
                    deck = self.engine.decks.get(slot)
                    if deck is not None:
                        self.assertTrue(
                            _pump_until_seek_resolved(self.engine, deck, timeout=5.0),
                            f"cycle {i} ({op}) never resolved: {self._snapshot()}",
                        )
                    if op == "rapid_seek":
                        # A second seek issued immediately after the
                        # first has already resolved -- the ordinary
                        # (not in-flight) back-to-back path, distinct
                        # from GatedSeekLifecycleCollisionTests' seek-
                        # during-genuinely-in-flight-seek scenario.
                        self.engine._seek_deck(slot, target + 0.3)
                        deck = self.engine.decks.get(slot)
                        if deck is not None:
                            self.assertTrue(
                                _pump_until_seek_resolved(self.engine, deck, timeout=5.0),
                                f"cycle {i} (rapid_seek #2) never resolved: {self._snapshot()}",
                            )

                elif op == "eject_recreate":
                    self.engine._eject_deck(slot)
                    self.assertTrue(
                        _pump_engine(self.engine, lambda: self.engine.decks.get(slot) is None, timeout=2.0),
                        f"cycle {i} (eject) never cleared slot: {self._snapshot()}",
                    )
                    self.engine._create_deck(slot, self.log_item, resume_position_ns=0)
                    self.assertTrue(
                        _pump_engine(
                            self.engine,
                            lambda: self.engine.decks[slot].media_buffer_count > 0,
                            timeout=3.0,
                        )
                    )

                elif op == "pause_resume":
                    self.engine._pause_deck(slot)
                    # _pause_deck's own pre-existing, unmodified sequence
                    # (unlink the deck's src pad from the mixer, THEN
                    # set_state(PAUSED)) can rarely race a real GStreamer
                    # streaming error ("not-linked") from the element
                    # still trying to push through the just-unlinked
                    # ghost pad -- confirmed directly (not a gated-seek
                    # interaction: gated_seek was already None here).
                    # The engine's own EXISTING, separately-tested error
                    # path (_on_main_bus_error -> _on_deck_error) handles
                    # that completely safely by retiring the deck, same
                    # as any other genuine pipeline error -- so `None`
                    # here is a real, already-safe outcome this test
                    # must tolerate, not a bug in the gated-seek
                    # lifecycle this hardening pass is about. The next
                    # cycle's own top-of-loop "ensure populated" check
                    # recovers it.
                    paused_or_recovered = _pump_engine(
                        self.engine,
                        lambda: self.engine.decks.get(slot) is None or self.engine.decks[slot].paused,
                        timeout=2.0,
                    )
                    self.assertTrue(paused_or_recovered, f"cycle {i} (pause) never resolved: {self._snapshot()}")
                    deck = self.engine.decks.get(slot)
                    if deck is not None and deck.paused:
                        self.engine._resume_deck(slot)
                        deck = self.engine.decks.get(slot)
                        if deck is not None:
                            self.assertTrue(
                                _pump_until_seek_resolved(self.engine, deck, timeout=5.0),
                                f"cycle {i} (resume) never resolved: {self._snapshot()}",
                            )

                elif op == "remove_recreate":
                    deck = self.engine.decks.get(slot)
                    if deck is not None:
                        self.engine._remove_deck(deck)
                        self.assertTrue(
                            _pump_engine(self.engine, lambda: deck.finished, timeout=2.0),
                            f"cycle {i} (remove) never completed: {self._snapshot()}",
                        )
                    self.engine._create_deck(slot, self.log_item, resume_position_ns=0)
                    self.assertTrue(
                        _pump_engine(
                            self.engine,
                            lambda: self.engine.decks[slot].media_buffer_count > 0,
                            timeout=3.0,
                        )
                    )

                snap = self._snapshot()
                self.assertLessEqual(snap["mixer_sinkpads"], 1, f"cycle {i} ({op}): {snap}")
                self.assertLessEqual(snap["deck_bin_map"], 1, f"cycle {i} ({op}): {snap}")
                self.assertLessEqual(snap["decks_occupied"], 1, f"cycle {i} ({op}): {snap}")
                self.assertEqual(snap["quarantined"], 0, f"cycle {i} ({op}): {snap}")

        final = self._snapshot()
        self.assertEqual(final, {
            "mixer_sinkpads": 1, "deck_bin_map": 1, "decks_occupied": 1, "quarantined": 0,
        })
        survivor = self.engine.decks[slot]
        self.assertIsNotNone(survivor)
        self.assertIsNone(survivor.gated_seek)
        self.assertFalse(survivor.finished)
        self.assertFalse(survivor.retirement_started)
        self.assertGreater(len(survivor.probe_handles), 0)

        # Let every per-slot teardown coordinator's queue (104 cycles'
        # worth of ordinary/eject/remove-driven NULL transitions, none
        # necessarily waited on individually above) actually finish
        # draining before checking it -- both for this assertion's own
        # sake and so no backlog is still running (and no
        # "deck-real-<slot>-test-worker"-named thread still alive) by
        # the time whatever test runs after this one starts.
        def _teardowns_fully_settled():
            for slot_name in ("A", "B"):
                snap = self.engine._deck_teardowns[slot_name].snapshot()
                if snap["queue_depth"] != 0 or snap["active_generation"] is not None:
                    return False
            return True

        self.assertTrue(_pump_engine(self.engine, _teardowns_fully_settled, timeout=10.0))

        # No abandoned/still-IN_FLIGHT worker left anywhere -- every
        # per-slot teardown coordinator is healthy (not poisoned) and
        # idle (nothing active, nothing queued).
        for slot_name in ("A", "B"):
            coordinator_snapshot = self.engine._deck_teardowns[slot_name].snapshot()
            self.assertFalse(coordinator_snapshot["poisoned"], f"slot {slot_name}: {coordinator_snapshot}")
            self.assertIsNone(coordinator_snapshot["active_generation"], f"slot {slot_name}: {coordinator_snapshot}")
            self.assertEqual(coordinator_snapshot["queue_depth"], 0, f"slot {slot_name}: {coordinator_snapshot}")

        # Engine genuinely still functional after 104 mixed cycles --
        # not just "resource counts look right", but a real subsequent
        # seek actually lands correctly. (main_pipeline's own zero-
        # timeout get_state() peek is a separately-confirmed-flaky
        # signal here: a bare set_state(PAUSED) on a plain, no-longer-
        # linked deck bin -- _pause_deck's own existing, unmodified
        # behavior, reproduced identically with heavy pause/resume
        # churn alone -- can leave GStreamer's cached top-level state
        # reporting stale/transitional values for several real seconds
        # even under active bus-draining, well after actual playback
        # has already recovered; not a regression this hardening pass
        # introduced, and not what this test is meant to verify.)
        self.engine._seek_deck(slot, 2.0)
        final_deck = self.engine.decks.get(slot)
        self.assertIsNotNone(final_deck)
        self.assertTrue(_pump_until_seek_resolved(self.engine, final_deck, timeout=5.0))
        ok, pos = final_deck.pipeline.query_position(Gst.Format.TIME)
        self.assertTrue(ok)
        self.assertAlmostEqual(pos / Gst.SECOND, 2.0, delta=0.05)
