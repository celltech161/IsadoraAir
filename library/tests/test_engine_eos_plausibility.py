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
decoding within ~2.7s -- the basis for SEEK_EOS_GUARD_SECONDS=3.0."""
import inspect
import threading
import time
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase

import library.services.engine as eng_module
from library.services.engine import DECK_STUCK_TIMEOUT_SECONDS, SEEK_EOS_GUARD_SECONDS, Deck


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
        audio with no recovery path. Static check: _on_deck_error calls
        _handle_deck_finished directly, not through
        _on_deck_eos_probed's position-gated path."""
        src = inspect.getsource(eng_module.PlaybackEngine._on_deck_error)
        self.assertIn("_handle_deck_finished", src)
        self.assertNotIn("_get_deck_position", src)


class SeekRejectionHandlingTests(TransactionTestCase):
    """seek_simple()'s boolean return value is checked at all three
    seek-application sites -- confirmed via an isolated repro that this
    does NOT catch the transient post-seek parser hiccup (seek_simple
    still returns True there), but it's real hygiene for the rarer,
    different case of a seek genuinely being rejected outright (wrong
    pipeline state, non-seekable source). Static-source checks: full
    behavioral coverage would need a real GStreamer pipeline, same
    reasoning the project already applies elsewhere (e.g.
    test_mark_song_requests_aired_gated_on_played_at_write_succeeding
    in test_request_scheduling_lifecycle.py)."""

    def test_create_deck_checks_auto_resume_seek_result(self):
        src = inspect.getsource(eng_module.PlaybackEngine._create_deck)
        self.assertIn("seek_ok = deck.pipeline.seek_simple", src)
        self.assertIn("if not seek_ok:", src)
        self.assertIn("deck.seeked_at = time.time()", src)

    def test_resume_deck_checks_seek_result(self):
        src = inspect.getsource(eng_module.PlaybackEngine._resume_deck)
        self.assertIn("seek_ok = new_deck.pipeline.seek_simple", src)
        self.assertIn("new_deck.seeked_at = time.time()", src)

    def test_seek_deck_checks_seek_result(self):
        src = inspect.getsource(eng_module.PlaybackEngine._seek_deck)
        self.assertIn("seek_ok = new_deck.pipeline.seek_simple", src)
        self.assertIn("new_deck.seeked_at = time.time()", src)

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
