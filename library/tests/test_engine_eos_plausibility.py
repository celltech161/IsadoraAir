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

_on_deck_eos_probed now checks the deck's actual position against its
track's recorded duration before trusting an EOS as genuine; one that
arrives far short of the expected duration is treated as implausible
(almost certainly this parser artifact, not a real end of stream) and
ignored rather than torn down. Reuses DECK_STUCK_TIMEOUT_SECONDS (the
existing margin _check_stuck_decks already uses for the opposite
direction -- past duration with no EOS) as "how much slop around the
expected duration is plausible" either way."""
import threading
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase

import library.services.engine as eng_module
from library.services.engine import DECK_STUCK_TIMEOUT_SECONDS, Deck


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


def make_deck(slot="A", duration_seconds=180.0, title="Test Track"):
    track = MagicMock(id=1, title=title, duration_seconds=duration_seconds)
    return Deck(slot=slot, track=track, log_item=MagicMock(), pipeline=MagicMock(), mixer_pad=MagicMock())


class EOSPlausibilityTests(TransactionTestCase):
    def _probe(self, stand_in, deck, position):
        deck_bin = object()
        stand_in._deck_bin_map[id(deck_bin)] = deck
        stand_in._get_deck_position = MagicMock(return_value=position)
        stand_in._handle_deck_finished = MagicMock()
        eng_module.PlaybackEngine._on_deck_eos_probed(stand_in, deck_bin)
        return stand_in._handle_deck_finished

    def test_implausible_eos_far_short_of_duration_is_ignored(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=7194.0)

        mock_finished = self._probe(stand_in, deck, position=6446.6)

        mock_finished.assert_not_called()
        self.assertFalse(deck.finished, "the deck must be left alone, not marked finished")

    def test_plausible_eos_near_true_end_is_honored(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=180.0)

        mock_finished = self._probe(stand_in, deck, position=179.5)

        mock_finished.assert_called_once_with(deck)

    def test_eos_exactly_at_the_plausibility_margin_is_honored(self):
        # duration - DECK_STUCK_TIMEOUT_SECONDS is the boundary -- "< "
        # not "<=" in the implementation, so exactly at the boundary is
        # still plausible.
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=180.0)

        mock_finished = self._probe(stand_in, deck, position=180.0 - DECK_STUCK_TIMEOUT_SECONDS)

        mock_finished.assert_called_once_with(deck)

    def test_eos_one_second_past_the_margin_is_implausible(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=180.0)

        mock_finished = self._probe(stand_in, deck, position=180.0 - DECK_STUCK_TIMEOUT_SECONDS - 1.0)

        mock_finished.assert_not_called()

    def test_missing_duration_falls_through_to_normal_handling(self):
        """Can't evaluate plausibility without a known duration --
        matches _check_stuck_decks' own `if not duration: continue`
        guard, same reasoning."""
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=None)

        mock_finished = self._probe(stand_in, deck, position=5.0)

        mock_finished.assert_called_once_with(deck)

    def test_already_finished_deck_short_circuits_before_position_check(self):
        stand_in = make_stand_in()
        deck = make_deck(duration_seconds=180.0)
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
        deck = make_deck(duration_seconds=7194.0)

        with patch.object(eng_module, "emit_event") as mock_emit:
            self._probe(stand_in, deck, position=6446.6)

        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["category"], "engine")
        self.assertEqual(mock_emit.call_args.kwargs["detail"]["track_id"], deck.track.id)

    def test_error_path_still_tears_down_regardless_of_position(self):
        """_on_deck_error (a genuine GStreamer pipeline error, not an
        EOS) must NOT go through this plausibility gate -- a real
        pipeline error deserves a full teardown regardless of position;
        silently ignoring it would leave a broken deck producing no
        audio with no recovery path. Static check: _on_deck_error calls
        _handle_deck_finished directly, not through
        _on_deck_eos_probed's position-gated path."""
        import inspect
        src = inspect.getsource(eng_module.PlaybackEngine._on_deck_error)
        self.assertIn("_handle_deck_finished", src)
        self.assertNotIn("_get_deck_position", src)
