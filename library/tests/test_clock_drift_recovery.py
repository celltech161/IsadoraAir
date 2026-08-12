"""1.1 spec (2026-08-11) -- clock-drift recovery: projecting the upcoming
hour's real takeover time from live deck state (_leading_deck_eta_seconds,
_project_upcoming_hour_target_duration) and threading the resulting
target_duration_seconds through _ensure_log_building/_build_hour_log_worker.

Uses bare PlaybackEngine stand-ins (bypassing __init__, same pattern as
test_hour_log_async_build.py's make_stand_in) with SimpleNamespace fake
decks -- no real GStreamer pipeline or Track/LogItem fixtures needed for
the pure projection-math tests. Only the clamping tests (which emit a
SystemEvent) touch the DB."""
import threading
import time
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

import library.services.engine as eng_module
from monitoring.models import SystemEvent


def make_stand_in():
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.decks = {"A": None, "B": None}
    obj._lock = threading.RLock()
    return obj


def make_fake_deck(next_start_seconds, duration_seconds, position, paused=False):
    """A minimal fake deck -- silence_primed=True routes _get_deck_
    position through its wall-clock branch (time.time() - started_at)
    rather than needing a real GStreamer pipeline; started_at is backed
    out from the desired `position` at construction time."""
    track = SimpleNamespace(next_start_seconds=next_start_seconds, duration_seconds=duration_seconds)
    return SimpleNamespace(
        track=track, paused=paused, paused_position=position if paused else 0.0,
        silence_primed=True, started_at=time.time() - position,
    )


class LeadingDeckEtaTests(TestCase):
    def test_no_decks_returns_zero(self):
        stand_in = make_stand_in()
        self.assertEqual(stand_in._leading_deck_eta_seconds(), 0.0)

    def test_both_decks_paused_returns_zero(self):
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(200, 210, 50, paused=True)
        self.assertEqual(stand_in._leading_deck_eta_seconds(), 0.0)

    def test_uses_next_start_seconds_when_set(self):
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=200, duration_seconds=210, position=150)
        eta = stand_in._leading_deck_eta_seconds()
        self.assertAlmostEqual(eta, 50, delta=1.0)

    def test_falls_back_to_duration_when_next_start_unset(self):
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=None, duration_seconds=210, position=150)
        eta = stand_in._leading_deck_eta_seconds()
        self.assertAlmostEqual(eta, 60, delta=1.0)

    def test_never_negative_when_past_effective_end(self):
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=100, duration_seconds=210, position=150)
        eta = stand_in._leading_deck_eta_seconds()
        self.assertEqual(eta, 0.0)

    def test_crossfade_overlap_uses_the_finishing_deck_not_slot_order(self):
        """Second-pass correction: during a brief crossfade overlap
        (both decks occupied and unpaused simultaneously), the deck
        that governs the NEXT queue-start decision is whichever is
        SOONEST to finish -- never simply whichever slot is encountered
        first in SLOTS order. Deck A here is the just-started INCOMING
        track (almost its full duration left); deck B is the almost-
        finished OUTGOING track. The projection must reflect B's much
        shorter remaining time, not A's, or clock-drift recovery would
        wildly overestimate how soon the upcoming hour can actually
        take over."""
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=300, duration_seconds=310, position=0)  # incoming, just started
        stand_in.decks["B"] = make_fake_deck(next_start_seconds=10, duration_seconds=20, position=0)  # outgoing, almost done
        eta = stand_in._leading_deck_eta_seconds()
        self.assertAlmostEqual(eta, 10, delta=1.0)

    def test_crossfade_overlap_finishing_deck_can_be_in_either_slot(self):
        """Same scenario with the finishing deck in slot A instead of B
        -- the selection must still be governed by remaining time, not
        by which slot happens to hold it."""
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=10, duration_seconds=20, position=0)  # outgoing, almost done
        stand_in.decks["B"] = make_fake_deck(next_start_seconds=300, duration_seconds=310, position=0)  # incoming, just started
        eta = stand_in._leading_deck_eta_seconds()
        self.assertAlmostEqual(eta, 10, delta=1.0)

    def test_invalid_deck_read_fails_safe_and_does_not_discard_the_other_deck(self):
        """A deck whose position query raises must be skipped, not
        crash the whole computation -- and a GOOD reading from the
        other occupied deck must still be used."""
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=300, duration_seconds=310, position=0)
        # Corrupt deck B so _get_deck_position raises on it.
        broken = make_fake_deck(next_start_seconds=10, duration_seconds=20, position=0)
        broken.track = None  # triggers the "lt is None" skip path
        stand_in.decks["B"] = broken
        eta = stand_in._leading_deck_eta_seconds()
        self.assertAlmostEqual(eta, 300, delta=1.0)

    def test_all_decks_invalid_fails_safe_to_zero(self):
        stand_in = make_stand_in()
        broken = make_fake_deck(next_start_seconds=10, duration_seconds=20, position=0)
        broken.track = None
        stand_in.decks["A"] = broken
        eta = stand_in._leading_deck_eta_seconds()
        self.assertEqual(eta, 0.0)

    def test_paused_deck_skipped_in_favor_of_playing_one(self):
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=300, duration_seconds=310, position=0, paused=True)
        stand_in.decks["B"] = make_fake_deck(next_start_seconds=40, duration_seconds=50, position=0)
        eta = stand_in._leading_deck_eta_seconds()
        self.assertAlmostEqual(eta, 40, delta=1.0)


class ProjectUpcomingHourTargetDurationTests(TestCase):
    def test_no_drift_returns_nominal_hour(self):
        """No leading deck (idle engine) -- eta 0, projected_start ==
        now; if now == nominal_start exactly, late_offset is 0 and the
        target is a full nominal hour."""
        stand_in = make_stand_in()
        nominal_start = timezone.localtime()
        target = stand_in._project_upcoming_hour_target_duration(nominal_start)
        # A tiny positive late_offset is expected -- real wall-clock time
        # advances microseconds between nominal_start being captured here
        # and _project_upcoming_hour_target_duration's own now=localtime()
        # call; only exact-3600 would be a coincidence.
        self.assertAlmostEqual(target, eng_module.NOMINAL_HOUR_SECONDS, delta=0.5)

    def test_late_projection_shortens_target(self):
        stand_in = make_stand_in()
        nominal_start = timezone.localtime()
        # Leading deck won't free up for 200s past nominal_start.
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=200, duration_seconds=210, position=0)
        target = stand_in._project_upcoming_hour_target_duration(nominal_start)
        self.assertAlmostEqual(target, eng_module.NOMINAL_HOUR_SECONDS - 200, delta=1.5)

    def test_early_projection_never_lengthens_target(self):
        """One-way only: nominal_start set in the FUTURE (leading deck
        frees up well before it) must not push target ABOVE nominal --
        late_offset floors at 0, it never goes negative to add time."""
        stand_in = make_stand_in()
        nominal_start = timezone.localtime() + timedelta(seconds=500)
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=10, duration_seconds=20, position=0)
        target = stand_in._project_upcoming_hour_target_duration(nominal_start)
        self.assertEqual(target, eng_module.NOMINAL_HOUR_SECONDS)

    def test_offset_clamped_to_max_and_event_emitted(self):
        stand_in = make_stand_in()
        nominal_start = timezone.localtime()
        # Leading deck won't free up for far longer than MAX_CLOCK_RECOVERY_SECONDS.
        huge = eng_module.MAX_CLOCK_RECOVERY_SECONDS + 300
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=huge, duration_seconds=huge + 10, position=0)
        target = stand_in._project_upcoming_hour_target_duration(nominal_start)
        self.assertEqual(target, eng_module.NOMINAL_HOUR_SECONDS - eng_module.MAX_CLOCK_RECOVERY_SECONDS)
        event = SystemEvent.objects.get(dedupe_key=f"engine|clock-drift-clamped|{nominal_start.isoformat()}")
        self.assertEqual(event.category, "engine")
        self.assertEqual(event.level, "warning")
        self.assertEqual(event.detail["clamped_to_seconds"], eng_module.MAX_CLOCK_RECOVERY_SECONDS)

    def test_offset_within_max_does_not_emit_clamp_event(self):
        stand_in = make_stand_in()
        nominal_start = timezone.localtime()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=100, duration_seconds=110, position=0)
        stand_in._project_upcoming_hour_target_duration(nominal_start)
        self.assertFalse(SystemEvent.objects.filter(dedupe_key=f"engine|clock-drift-clamped|{nominal_start.isoformat()}").exists())

    def test_unexpected_exception_fails_safe_to_nominal_target(self):
        """Second-pass correction: a bug/failure anywhere in the
        projection itself (not just a per-deck read, which
        _leading_deck_eta_seconds already contains) must never shorten
        the upcoming hour -- falls back to NOMINAL_HOUR_SECONDS and
        records a diagnostic event."""
        stand_in = make_stand_in()
        nominal_start = timezone.localtime()
        with patch.object(stand_in, "_leading_deck_eta_seconds", side_effect=RuntimeError("boom")):
            target = stand_in._project_upcoming_hour_target_duration(nominal_start)
        self.assertEqual(target, eng_module.NOMINAL_HOUR_SECONDS)
        self.assertTrue(SystemEvent.objects.filter(
            dedupe_key=f"engine|clock-drift-projection-failed|{nominal_start.isoformat()}",
        ).exists())


class TargetDurationThreadingTests(TestCase):
    """_ensure_log_building/_build_hour_log_worker must accept and pass
    through target_duration_seconds all the way to
    build_and_approve_hour_log_locked."""

    def test_ensure_log_building_passes_target_duration_to_worker(self):
        stand_in = make_stand_in()
        stand_in._building_hours = set()
        captured = {}

        def fake_worker(target_date, target_hour, target_duration_seconds=eng_module.NOMINAL_HOUR_SECONDS):
            captured["target_duration_seconds"] = target_duration_seconds

        with patch.object(eng_module.PlaylistLog.objects, "filter") as mock_filter, \
             patch.object(stand_in, "_build_hour_log_worker", side_effect=fake_worker):
            mock_filter.return_value.exists.return_value = False
            stand_in._ensure_log_building(date(2027, 5, 1), 10, target_duration_seconds=3100)

        deadline = time.time() + 5
        while "target_duration_seconds" not in captured and time.time() < deadline:
            time.sleep(0.02)

        self.assertEqual(captured.get("target_duration_seconds"), 3100)
