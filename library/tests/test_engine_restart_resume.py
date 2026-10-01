"""P0 restart recovery regressions for exact on-air occurrence restoration."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Track
from library.services.engine import Deck, PlaybackEngine
from library.tests.test_continuation_hour_orchestration import make_stand_in
from library.tests.test_engine_runtime_commit import make_minimal_stand_in


class RestartResumeFixture(TransactionTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-restart-resume.")
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "engine_state.json"
        state_patcher = patch.object(eng_module, "STATE_PATH", self.state_path)
        state_patcher.start()
        self.addCleanup(state_patcher.stop)

        kind = CategoryKind.objects.create(code="restart", name="Restart")
        self.category = Category.objects.create(code="RESTART", name="Restart", kind=kind)
        self.artist = Artist.objects.create(name="Restart Artist")
        self.log = PlaylistLog.objects.create(
            date=date(2026, 10, 1), hour=0, status="approved"
        )
        self.track_n = self.make_track("Stories Across The Plains - Saturdays at Noon")
        self.track_next = self.make_track("Oak Grove Radio - Current Temp (Max)")
        started = timezone.now() - timedelta(seconds=4)
        # The real incident: N+1's nominal time is already in the past even
        # though N is the occurrence actually producing program audio.
        self.item_n = self.make_item(
            17, self.track_n, played_at=started,
            scheduled_time=started - timedelta(seconds=30),
        )
        self.item_next = self.make_item(
            18, self.track_next, played_at=None,
            scheduled_time=started - timedelta(seconds=4),
        )

    def make_track(self, title, *, duration=180.0, next_start=175.0):
        path = Path(self.temp_dir.name) / f"{title.replace(' ', '-')}.wav"
        path.touch()
        return Track.objects.create(
            filepath=str(path), filename=path.name, title=title,
            artist=self.artist, category=self.category, ready2air=True,
            duration_seconds=duration, next_start_seconds=next_start,
        )

    def make_item(self, position, track, *, played_at, scheduled_time):
        return LogItem.objects.create(
            playlist_log=self.log, position=position,
            scheduled_time=scheduled_time, track=track,
            track_title=track.title, track_artist=self.artist.name,
            category=self.category, played_at=played_at,
        )

    def engine(self):
        obj = make_stand_in()
        obj._engine_session_id = "new-engine-session"
        obj._resume_hint = None
        return obj

    def write_resume_state(
        self, *, item=None, track_id=None, saved_position=4.0,
        age=0.5, transport="STOPPED", actually_playing=True,
        session_id="previous-engine-session",
    ):
        item = item or self.item_n
        payload = {
            "transport": transport,
            "timestamp": time.time() - age,
            "engine_session_id": session_id,
            "log_id": self.log.id,
            "decks": {},
            "resume": {
                "version": eng_module.RESUME_STATE_VERSION,
                "session_id": session_id,
                "actually_playing": actually_playing,
                "playlist_log_id": self.log.id,
                "log_item_id": item.id,
                "log_position": item.position,
                "track_id": track_id if track_id is not None else item.track_id,
                "slot": "A",
                "deck_generation": 7,
                "saved_position": saved_position,
            },
        }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")


class RestartTargetSelectionTests(RestartResumeFixture):
    def test_r0098_legacy_under_five_second_snapshot_bootstraps_upgrade(self):
        """The first fixed startup reads the state written by r0098 itself."""
        payload = {
            "transport": "STOPPED",
            "timestamp": time.time() - 0.5,
            "log_id": self.log.id,
            "decks": {
                "A": {
                    "track_id": self.track_n.id,
                    "log_item_id": self.item_n.id,
                    "position": 4.0,
                    "category": self.category.code,
                    "paused": False,
                },
                "B": None,
            },
        }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        engine = self.engine()

        engine._read_resume_hint()
        engine._load_current_hour_log()
        engine._apply_resume_hint_queue_rewind()

        self.assertEqual(engine._resume_hint["log_item_id"], self.item_n.id)
        self.assertEqual(engine._queue_cursor, 0)
        self.assertAlmostEqual(engine._resume_hint["position"], 3.75)

    def test_kogr_position_17_playing_position_18_next_restores_17(self):
        self.write_resume_state(saved_position=4.0)
        engine = self.engine()

        engine._read_resume_hint()
        engine._load_current_hour_log()
        # played_at-based loading alone advances to N+1.
        self.assertEqual(engine._queue_cursor, 1)
        engine._apply_resume_hint_queue_rewind()

        self.assertEqual(engine._queue_cursor, 0)
        self.assertEqual(engine._resume_hint["log_item_id"], self.item_n.id)
        self.assertAlmostEqual(engine._resume_hint["position"], 3.75)
        selected, forced = engine._next_queue_item()
        self.assertFalse(forced)
        self.assertEqual(selected.id, self.item_n.id)
        following, forced = engine._next_queue_item()
        self.assertFalse(forced)
        self.assertEqual(following.id, self.item_next.id)

    def test_prepared_next_deck_never_beats_actual_playing_deck(self):
        engine = self.engine()
        decks = {
            "A": {
                "slot": "A", "playlist_log_id": self.log.id,
                "log_item_id": self.item_n.id, "log_position": 17,
                "track_id": self.track_n.id, "position": 4.0,
                "actually_playing": True, "category": self.category.code,
                "generation": 1, "media_buffers": 100,
            },
            "B": {
                "slot": "B", "playlist_log_id": self.log.id,
                "log_item_id": self.item_next.id, "log_position": 18,
                "track_id": self.track_next.id, "position": 0.0,
                "actually_playing": False, "category": self.category.code,
                "generation": 2, "media_buffers": 0,
            },
        }

        resume = engine._build_resume_state(
            decks, captured_at=time.time(), transport="PLAYING"
        )

        self.assertEqual(resume["log_item_id"], self.item_n.id)
        self.assertEqual(resume["track_id"], self.track_n.id)

    def test_hint_loads_exact_prior_log_instead_of_wall_clock_log(self):
        current_log = PlaylistLog.objects.create(
            date=self.log.date, hour=1, status="approved"
        )
        current_track = self.make_track("Current wall-clock hour")
        LogItem.objects.create(
            playlist_log=current_log, position=0, scheduled_time=timezone.now(),
            track=current_track, category=self.category,
        )
        self.write_resume_state(saved_position=4.0)
        engine = self.engine()
        engine._read_resume_hint()
        fake_now = timezone.make_aware(timezone.datetime(2026, 10, 1, 1, 1))

        with patch.object(eng_module.timezone, "localtime", return_value=fake_now):
            engine._load_current_hour_log()
        engine._apply_resume_hint_queue_rewind()

        self.assertEqual(engine.current_log.id, self.log.id)
        self.assertEqual(engine.log_items[engine._queue_cursor].id, self.item_n.id)

    def test_no_resume_state_retains_safe_first_unplayed_behavior(self):
        engine = self.engine()
        engine._read_resume_hint()
        engine._load_log_for(self.log.date, self.log.hour)

        self.assertIsNone(engine._resume_hint)
        self.assertEqual(engine.log_items[engine._queue_cursor].id, self.item_next.id)

    def test_deleted_log_item_fails_closed(self):
        self.write_resume_state()
        self.item_n.delete()
        engine = self.engine()
        engine._read_resume_hint()
        self.assertIsNone(engine._resume_hint)

    def test_track_identity_mismatch_fails_closed(self):
        self.write_resume_state(track_id=self.track_next.id)
        engine = self.engine()
        engine._read_resume_hint()
        self.assertIsNone(engine._resume_hint)

    def test_prepared_unstarted_occurrence_fails_closed(self):
        self.write_resume_state(item=self.item_next, saved_position=0.0)
        engine = self.engine()
        engine._read_resume_hint()
        self.assertIsNone(engine._resume_hint)

    def test_stale_and_same_session_state_are_rejected(self):
        engine = self.engine()
        self.write_resume_state(age=eng_module.RESUME_STATE_MAX_AGE_SECONDS + 1)
        engine._read_resume_hint()
        self.assertIsNone(engine._resume_hint)

        self.write_resume_state(session_id=engine._engine_session_id)
        engine._read_resume_hint()
        self.assertIsNone(engine._resume_hint)

    def test_recent_playing_crash_snapshot_is_accepted(self):
        self.write_resume_state(transport="PLAYING", age=0.3)
        engine = self.engine()
        engine._read_resume_hint()
        self.assertEqual(engine._resume_hint["capture_kind"], "recent_crash_snapshot")
        self.assertEqual(engine._resume_hint["log_item_id"], self.item_n.id)

    def test_crash_snapshot_age_does_not_skip_forward(self):
        self.write_resume_state(transport="PLAYING", saved_position=40.0, age=30.0)
        engine = self.engine()
        engine._read_resume_hint()
        self.assertEqual(engine._resume_hint["position"], 39.75)


class ResumePositionPolicyTests(RestartResumeFixture):
    def policy(self, saved):
        return PlaybackEngine._sanitize_resume_position(self.item_n, saved)

    def test_early_and_mid_track_positions_replay_small_overlap(self):
        self.assertEqual(self.policy(0.1)[0], 0.0)
        self.assertEqual(self.policy(4.0)[0], 3.75)
        self.assertEqual(self.policy(90.0)[0], 89.75)

    def test_near_end_and_beyond_duration_clamp_before_transition(self):
        self.assertEqual(self.policy(174.9)[0], 174.65)
        self.assertEqual(self.policy(999.0)[0], 174.75)
        self.assertEqual(self.policy(175.0)[0], 174.75)

    def test_missing_duration_falls_back_to_same_item_at_zero(self):
        self.track_n.duration_seconds = None
        self.track_n.next_start_seconds = None
        self.track_n.save(update_fields=["duration_seconds", "next_start_seconds"])
        self.item_n.track = self.track_n
        target, reason = self.policy(50.0)
        self.assertEqual(target, 0.0)
        self.assertEqual(reason, "same_item_zero_missing_duration")

    def test_invalid_position_is_rejected(self):
        for value in (-1, float("inf"), float("nan"), "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.policy(value)


class ResumeStatePersistenceTests(RestartResumeFixture):
    def test_graceful_final_write_persists_exact_playing_identity(self):
        engine = make_minimal_stand_in()
        engine._engine_session_id = "persisted-session"
        engine.current_log = self.log
        engine.log_items = [self.item_n, self.item_next]
        engine._queue_cursor = 2
        deck = Deck(
            "A", self.track_n, self.item_n, MagicMock(), MagicMock(),
            generation=9,
        )
        deck.media_buffer_count = 25
        engine.decks = {"A": deck, "B": None}
        engine._get_deck_position = MagicMock(return_value=4.0)

        engine._write_state(transport="STOPPED")
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["transport"], "STOPPED")
        self.assertEqual(payload["engine_session_id"], "persisted-session")
        self.assertEqual(payload["resume"]["playlist_log_id"], self.log.id)
        self.assertEqual(payload["resume"]["log_item_id"], self.item_n.id)
        self.assertEqual(payload["resume"]["track_id"], self.track_n.id)
        self.assertEqual(payload["resume"]["saved_position"], 4.0)
        self.assertTrue(payload["resume"]["actually_playing"])


class PreSyncSeekGateContractTests(TransactionTestCase):
    def test_seek_gate_callback_precedes_playing_parent_sync(self):
        source = __import__("inspect").getsource(PlaybackEngine._create_deck)
        self.assertIn("before_sync(deck)", source)
        self.assertLess(
            source.index("before_sync(deck)"),
            source.index("deck_bin.sync_state_with_parent()"),
        )

    def test_begin_gated_seek_uses_only_pre_sync_gate_seam(self):
        source = __import__("inspect").getsource(PlaybackEngine._begin_gated_seek)
        self.assertIn("before_sync=_arm_gate_before_sync", source)
        self.assertNotIn("new_deck.pipeline.get_static_pad", source)
