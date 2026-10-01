"""P0 restart recovery regressions for exact on-air occurrence restoration."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import date, time as dt_time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.models import (
    Artist, Category, CategoryKind, LogItem, PlaylistLog, Rotation,
    ScheduleBlock, Track,
)
from library.services.engine import Deck, PlaybackEngine
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.tests.test_continuation_hour_orchestration import make_stand_in
from library.tests.test_engine_runtime_commit import make_minimal_stand_in


class RestartResumeFixture(TransactionTestCase):
    def setUp(self):
        # A TransactionTestCase flush removes the migration-seeded profile
        # singleton that the engine's schedule classification resolves against.
        ensure_schedule_profile_state()
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
        session_id="previous-engine-session", queue_log=None,
        queue_resume_cursor=None, queue_resume_next_id="absent",
    ):
        item = item or self.item_n
        payload = {
            "transport": transport,
            "timestamp": time.time() - age,
            "engine_session_id": session_id,
            "log_id": (queue_log or self.log).id,
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
        if queue_resume_next_id != "absent":
            payload["queue_resume_cursor"] = queue_resume_cursor
            payload["queue_resume_next_log_item_id"] = queue_resume_next_id
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    def recover(self, engine, now=None):
        """The real startup sequence up to (not including) deck creation."""
        engine._read_resume_hint()
        if now is None:
            now = timezone.make_aware(timezone.datetime(2026, 10, 1, 0, 59, 40))
        with patch.object(eng_module.timezone, "localtime", return_value=now):
            engine._load_current_hour_log()
        engine._install_resume_occurrence()
        return now

    def start_tracks(self, engine, count):
        """Drive the REAL _start_next_track/_next_queue_item path (the one the
        crossfade trigger and EOS use) with only GStreamer deck creation
        stubbed.  Returns [(log_item_id, was_resume_hint_match), ...]."""
        created = []
        engine._start_next_track = PlaybackEngine._start_next_track.__get__(engine)
        engine._free_slot = lambda: next(
            (slot for slot in ("A", "B") if engine.decks.get(slot) is None), None
        )
        engine._deck_slot_available = lambda slot: True
        engine._maybe_insert_dedication_intro = lambda item: item

        def fake_create_deck(slot, log_item, *args, **kwargs):
            hint = getattr(engine, "_resume_hint", None)
            matched = bool(hint and hint.get("log_item_id") == log_item.id)
            if hint:
                engine._resume_hint = None  # the real _create_deck consumes it
            created.append((log_item.id, matched))
            engine.decks[slot] = None  # the track "ends" before the next start
            return MagicMock()

        engine._create_deck = fake_create_deck
        with patch.object(eng_module, "maybe_schedule_song_request", return_value=None):
            for _ in range(count):
                engine._start_next_track()
        return created


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

        self.recover(engine)

        self.assertEqual(engine._resume_hint["log_item_id"], self.item_n.id)
        self.assertEqual(engine._resume_hint["playlist_log_id"], self.log.id)
        self.assertEqual(engine._resume_item.id, self.item_n.id)
        self.assertEqual(engine._queue_cursor, 1)  # the queue continues after N
        self.assertAlmostEqual(engine._resume_hint["position"], 3.75)

    def test_kogr_position_17_playing_position_18_next_restores_17(self):
        self.write_resume_state(saved_position=4.0)
        engine = self.engine()

        self.recover(engine)

        self.assertEqual(engine._resume_hint["log_item_id"], self.item_n.id)
        self.assertAlmostEqual(engine._resume_hint["position"], 3.75)
        # N is a DIRECT resume item; the queue itself sits at N+1 and was
        # never rewound or relocated.
        self.assertEqual(engine._queue_cursor, 1)
        selected, forced = engine._next_queue_item()
        self.assertTrue(forced)  # direct resume item: skips request scheduling
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

    def test_hint_without_queue_identity_uses_occurrence_log_not_wall_clock(self):
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
        self.recover(engine, now=timezone.make_aware(timezone.datetime(2026, 10, 1, 1, 1)))

        # No saved queue identity and nothing newer is known: the
        # occurrence's own log is the queue, never the wall-clock hour.
        self.assertEqual(engine.current_log.id, self.log.id)
        self.assertEqual(engine._resume_item.id, self.item_n.id)
        self.assertEqual(engine.log_items[engine._queue_cursor].id, self.item_next.id)

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
    """The hard bound is the media duration, never next_start_seconds."""

    def policy(self, saved, *, duration=180.0, next_start=175.0):
        self.track_n.duration_seconds = duration
        self.track_n.next_start_seconds = next_start
        self.item_n.track = self.track_n
        return PlaybackEngine._sanitize_resume_position(self.item_n, saved)

    def test_early_and_mid_track_positions_replay_small_overlap(self):
        self.assertEqual(self.policy(0.1)[0], 0.0)
        self.assertEqual(self.policy(4.0)[0], 3.75)
        self.assertEqual(self.policy(90.0)[0], 89.75)

    def test_position_before_next_start_replays_small_overlap(self):
        self.assertEqual(self.policy(179.9, duration=240.0, next_start=180.0)[0], 179.65)

    def test_position_just_after_next_start_is_not_rewound(self):
        target, reason = self.policy(180.5, duration=240.0, next_start=180.0)
        self.assertEqual(target, 180.25)
        self.assertEqual(reason, "saved_position_with_overlap")

    def test_position_well_after_next_start_but_before_duration_is_preserved(self):
        # A manual hold / delayed handoff legitimately plays past next-start.
        target, reason = self.policy(205.0, duration=240.0, next_start=180.0)
        self.assertEqual(target, 204.75)
        self.assertEqual(reason, "saved_position_with_overlap")

    def test_position_at_or_past_media_end_clamps_before_the_end(self):
        for saved in (240.0, 239.9, 999.0):
            with self.subTest(saved=saved):
                target, _reason = self.policy(saved, duration=240.0, next_start=180.0)
                self.assertLessEqual(target, 239.75)
                self.assertGreaterEqual(target, 239.0)
        self.assertEqual(self.policy(999.0, duration=240.0, next_start=180.0), (239.75, "clamped_before_media_end"))

    def test_missing_duration_falls_back_to_same_item_at_zero_even_with_next_start(self):
        for duration in (None, 0, -5, float("nan")):
            with self.subTest(duration=duration):
                target, reason = self.policy(50.0, duration=duration, next_start=175.0)
                self.assertEqual(target, 0.0)
                self.assertEqual(reason, "same_item_zero_missing_duration")

    def test_end_to_end_late_position_survives_a_snapshot_read(self):
        self.track_n.duration_seconds = 240.0
        self.track_n.next_start_seconds = 180.0
        self.track_n.save(update_fields=["duration_seconds", "next_start_seconds"])
        self.write_resume_state(saved_position=205.0, transport="PLAYING")
        engine = self.engine()
        engine._read_resume_hint()
        self.assertEqual(engine._resume_hint["position"], 204.75)

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


# ---------------------------------------------------------------------------
# Sol correction pass -- Fixes 1-4
# ---------------------------------------------------------------------------

class TwoHourFixture(RestartResumeFixture):
    """Old hour 00:00 (N=17 on air, N+1=18, N+2=19) and an already-approved
    01:00 queue (X0, X1) -- the early-rollover shape."""

    def setUp(self):
        super().setUp()
        started = self.item_n.played_at
        self.track_n2 = self.make_track("Third Old Hour Track")
        self.item_n2 = self.make_item(
            19, self.track_n2, played_at=None, scheduled_time=started + timedelta(seconds=300),
        )
        self.new_log = PlaylistLog.objects.create(
            date=self.log.date, hour=1, status="approved"
        )
        self.x_tracks = [self.make_track(f"New Hour {i}") for i in range(2)]
        self.x_items = [
            LogItem.objects.create(
                playlist_log=self.new_log, position=i,
                scheduled_time=started + timedelta(minutes=60 + i), track=track,
                track_title=track.title, track_artist=self.artist.name,
                category=self.category,
            )
            for i, track in enumerate(self.x_tracks)
        ]


class EarlyRolloverQueueSeparationTests(TwoHourFixture):
    """FIX 1 -- the interrupted occurrence and the active queue are separate."""

    def test_old_hour_occurrence_resumes_then_the_committed_next_hour_queue_continues(self):
        # 00:59:40: old-hour N physically playing; the engine already rolled
        # over: current_log = 01:00, queue at its first item.
        self.write_resume_state(
            saved_position=4.0, queue_log=self.new_log,
            queue_resume_cursor=0, queue_resume_next_id=self.x_items[0].id,
        )
        engine = self.engine()

        now = self.recover(engine)

        self.assertEqual(engine._resume_hint["playlist_log_id"], self.log.id)  # occurrence: OLD log
        self.assertEqual(engine.current_log.id, self.new_log.id)  # queue: 01:00, not re-pointed
        self.assertEqual(engine._resume_item.id, self.item_n.id)
        self.assertEqual(engine._queue_cursor, 0)
        self.assertEqual(engine._current_hour_schedule_state(now)["state"], "early_rollover")

        # Trigger/EOS BEFORE the 10-second orchestration tick: the real
        # next-item path.  The tick's own queue action is a no-op here too.
        created = self.start_tracks(engine, 1)
        engine._advance_to_next_hour_log(self.log.date, 1)
        self.assertEqual(engine.current_log.id, self.new_log.id)
        created += self.start_tracks(engine, 2)

        self.assertEqual(
            created,
            [(self.item_n.id, True), (self.x_items[0].id, False), (self.x_items[1].id, False)],
        )
        # No old-log N+1 / N+2 ever airs.
        aired = {item_id for item_id, _ in created}
        self.assertNotIn(self.item_next.id, aired)
        self.assertNotIn(self.item_n2.id, aired)

    def test_partial_takeover_restart_keeps_old_occurrence_and_new_active_queue_separate(self):
        profile = ensure_schedule_profile_state().active_profile
        rotation = Rotation.objects.create(name="Partial Restart Rotation")
        ScheduleBlock.objects.create(
            profile=profile, specific_date=self.log.date,
            start_time=dt_time(1, 30), end_time=dt_time(2, 0), rotation=rotation,
        )
        takeover = timezone.make_aware(
            timezone.datetime.combine(self.log.date, dt_time(1, 30))
        )
        for offset, item in enumerate(self.x_items):
            item.scheduled_time = takeover + timedelta(minutes=offset)
            item.save(update_fields=["scheduled_time"])

        self.write_resume_state(
            saved_position=4.0, queue_log=self.new_log,
            queue_resume_cursor=0, queue_resume_next_id=self.x_items[0].id,
        )
        engine = self.engine()
        now = timezone.make_aware(
            timezone.datetime.combine(self.log.date, dt_time(1, 35))
        )
        self.recover(engine, now=now)

        self.assertEqual(engine._resume_hint["playlist_log_id"], self.log.id)
        self.assertEqual(engine.current_log.id, self.new_log.id)
        self.assertEqual(engine._resume_item.id, self.item_n.id)
        self.assertEqual(engine._queue_cursor, 0)
        self.assertEqual(engine._current_hour_schedule_state(now)["state"], "partial_due")

        created = self.start_tracks(engine, 3)
        self.assertEqual(
            created,
            [(self.item_n.id, True), (self.x_items[0].id, False), (self.x_items[1].id, False)],
        )
        self.assertNotIn(self.item_next.id, {item_id for item_id, _ in created})
        self.assertNotIn(self.item_n2.id, {item_id for item_id, _ in created})

    def test_an_older_saved_queue_never_replaces_a_newer_occurrence_log(self):
        # Occurrence belongs to 01:00; the saved queue claims 00:00. An older
        # queue must not win: fall back to the occurrence's own (newer) log.
        item_x0 = self.x_items[0]
        item_x0.played_at = timezone.now() - timedelta(seconds=3)
        item_x0.save(update_fields=["played_at"])
        self.write_resume_state(
            item=item_x0, saved_position=3.0, queue_log=self.log,
            queue_resume_cursor=1, queue_resume_next_id=self.item_n2.id,
        )
        # write_resume_state stamps the occurrence's log; patch it truthfully.
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["resume"]["playlist_log_id"] = self.new_log.id
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        engine = self.engine()

        self.recover(engine, now=timezone.make_aware(timezone.datetime(2026, 10, 1, 1, 0, 5)))

        self.assertIsNone(engine._resume_hint["queue"])
        self.assertEqual(engine.current_log.id, self.new_log.id)
        self.assertEqual(engine._resume_item.id, item_x0.id)
        created = self.start_tracks(engine, 2)
        self.assertEqual(created[0][0], item_x0.id)
        self.assertEqual(created[1][0], self.x_items[1].id)

    def test_prepared_successor_is_returned_to_the_queue_not_skipped(self):
        # N actually playing, N+1 only PREPARED (the cursor already moved past
        # it).  The writer persists the queue's true next item: N+1.
        engine = make_minimal_stand_in()
        engine._engine_session_id = "writer"
        engine.current_log = self.log
        engine.log_items = list(self.log.items.order_by("position"))
        engine._queue_cursor = 2  # past N and the prepared N+1
        n_item, next_item = engine.log_items[0], engine.log_items[1]
        deck_a = Deck("A", self.track_n, n_item, MagicMock(), MagicMock(), generation=1)
        deck_a.media_buffer_count = 80
        deck_b = Deck("B", self.track_next, next_item, MagicMock(), MagicMock(), generation=2)
        deck_b.media_buffer_count = 0
        engine.decks = {"A": deck_a, "B": deck_b}
        engine._get_deck_position = lambda deck: 4.0 if deck.slot == "A" else 0.0

        engine._write_state(transport="STOPPED")
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["resume"]["log_item_id"], self.item_n.id)
        self.assertEqual(payload["queue_cursor"], 2)  # existing UI meaning unchanged
        self.assertEqual(payload["queue_resume_cursor"], 1)
        self.assertEqual(payload["queue_resume_next_log_item_id"], self.item_next.id)

        fresh = self.engine()
        self.recover(fresh)
        created = self.start_tracks(fresh, 3)
        self.assertEqual([i for i, _ in created], [self.item_n.id, self.item_next.id, self.item_n2.id])

    def test_writer_persists_queue_identity_separately_from_the_occurrence(self):
        engine = make_minimal_stand_in()
        engine._engine_session_id = "writer"
        engine.current_log = self.new_log  # active queue: 01:00
        engine.log_items = list(self.new_log.items.order_by("position"))
        engine._queue_cursor = 0
        deck = Deck("A", self.track_n, self.item_n, MagicMock(), MagicMock(), generation=3)
        deck.media_buffer_count = 40
        engine.decks = {"A": deck, "B": None}
        engine._get_deck_position = lambda _deck: 4.0

        engine._write_state(transport="STOPPED")
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["resume"]["playlist_log_id"], self.log.id)
        self.assertEqual(payload["log_id"], self.new_log.id)
        self.assertEqual(payload["queue_resume_cursor"], 0)
        self.assertEqual(payload["queue_resume_next_log_item_id"], self.x_items[0].id)

    def test_successor_that_aired_after_the_last_snapshot_never_replays(self):
        """The real race: snapshot says N on air with N+1 next; N+1 then begins
        airing (played_at commits) and the engine dies before the next state
        write.  Recovery rejects the stale saved identity, resumes N, and the
        queue continues at N+2 -- N+1 is never handed out again."""
        self.write_resume_state(
            queue_log=self.log, queue_resume_cursor=1,
            queue_resume_next_id=self.item_next.id,
        )
        # AFTER the snapshot was written:
        self.item_next.played_at = timezone.now()
        self.item_next.save(update_fields=["played_at"])
        engine = self.engine()

        self.recover(engine)

        self.assertEqual(engine._resume_hint["queue"]["next_log_item_id"], self.item_next.id)
        self.assertIsNone(engine._validated_saved_queue_cursor(engine._resume_hint["queue"]))  # rejected
        self.assertEqual(engine._resume_hint["queue_source"], "after_occurrence")
        self.assertEqual(engine._resume_item.id, self.item_n.id)
        self.assertEqual(engine._queue_cursor, 2)  # N+2, not N+1
        # Drive the real next-item path to exhaustion of this log.
        handed_out = []
        while True:
            item, _forced = engine._next_queue_item()
            if item is None or item.playlist_log_id != self.log.id:
                break
            handed_out.append(item.id)
        self.assertEqual(handed_out, [self.item_n.id, self.item_n2.id])
        self.assertNotIn(self.item_next.id, handed_out)

    def test_stale_successor_race_through_the_real_start_path(self):
        self.write_resume_state(
            queue_log=self.log, queue_resume_cursor=1,
            queue_resume_next_id=self.item_next.id,
        )
        self.item_next.played_at = timezone.now()
        self.item_next.save(update_fields=["played_at"])
        engine = self.engine()
        self.recover(engine)
        created = self.start_tracks(engine, 2)
        self.assertEqual(created, [(self.item_n.id, True), (self.item_n2.id, False)])

    def test_a_genuinely_unplayed_successor_still_follows_the_occurrence(self):
        # No usable saved identity, N+1 never aired: the safe cursor is N+1.
        self.write_resume_state(queue_log=self.log)
        engine = self.engine()
        self.recover(engine)
        created = self.start_tracks(engine, 3)
        self.assertEqual([i for i, _ in created], [self.item_n.id, self.item_next.id, self.item_n2.id])

    def test_the_fallback_never_moves_behind_the_resumed_occurrence(self):
        # A strange unplayed row positioned BEFORE N: the loader's cursor
        # points at it, but recovery must continue after N and never hand it out.
        early_track = self.make_track("Strange Earlier Row")
        early = self.make_item(
            10, early_track, played_at=None, scheduled_time=timezone.now() - timedelta(minutes=5),
        )
        self.write_resume_state(queue_log=self.log)
        engine = self.engine()
        self.recover(engine)
        self.assertEqual(engine.log_items[engine._queue_cursor].id, self.item_next.id)
        created = self.start_tracks(engine, 2)
        self.assertEqual([i for i, _ in created], [self.item_n.id, self.item_next.id])
        self.assertNotIn(early.id, [i for i, _ in created])

    def test_a_saved_cursor_at_or_before_the_occurrence_cannot_replay_it(self):
        self.write_resume_state(
            queue_log=self.log, queue_resume_cursor=0, queue_resume_next_id=self.item_n.id,
        )
        engine = self.engine()
        self.recover(engine)
        created = self.start_tracks(engine, 3)
        ids = [i for i, _ in created]
        self.assertEqual(ids.count(self.item_n.id), 1)
        self.assertEqual(ids[:2], [self.item_n.id, self.item_next.id])

    def test_a_forced_copy_of_the_resumed_occurrence_cannot_air_twice(self):
        self.write_resume_state()
        engine = self.engine()
        self.recover(engine)
        engine._forced_next_items = [self.item_n]  # e.g. a re-armed dedication intro
        created = self.start_tracks(engine, 2)
        self.assertEqual([i for i, _ in created], [self.item_n.id, self.item_next.id])


class TrueCrossfadePolicyTests(TwoHourFixture):
    """FIX 2 -- both decks genuinely contributing at snapshot time."""

    def deck_entry(self, slot, item, *, generation, position, playing=True, category=None):
        return {
            "slot": slot, "playlist_log_id": item.playlist_log_id,
            "log_item_id": item.id, "log_position": item.position,
            "track_id": item.track_id, "position": position,
            "actually_playing": playing, "generation": generation,
            "media_buffers": 100 if playing else 0,
            "air_started_at": item.played_at.timestamp() if item.played_at else None,
            "category": category or self.category.code,
        }

    def air(self, item, seconds_ago):
        item.played_at = timezone.now() - timedelta(seconds=seconds_ago)
        item.save(update_fields=["played_at"])
        return item

    def test_outgoing_actual_with_incoming_prepared_only_restores_the_outgoing(self):
        decks = {
            "A": self.deck_entry("A", self.item_n, generation=1, position=170.0),
            "B": self.deck_entry("B", self.item_next, generation=2, position=0.0, playing=False),
        }
        resume = make_minimal_stand_in()._build_resume_state(decks, captured_at=0, transport="PLAYING")
        self.assertEqual(resume["log_item_id"], self.item_n.id)

    def test_two_actually_playing_decks_restore_the_newer_incoming_occurrence(self):
        self.air(self.item_n, 175)
        self.air(self.item_next, 5)
        decks = {
            "A": self.deck_entry("A", self.item_n, generation=1, position=175.0),
            "B": self.deck_entry("B", self.item_next, generation=2, position=5.0),
        }
        resume = make_minimal_stand_in()._build_resume_state(decks, captured_at=0, transport="PLAYING")
        self.assertEqual(resume["log_item_id"], self.item_next.id)
        self.assertEqual(resume["saved_position"], 5.0)  # the winner's OWN position
        self.assertEqual(resume["slot"], "B")

    def test_the_winner_is_decided_by_air_start_not_deck_generation_or_slot(self):
        # A manual seek replaced the OUTGOING deck, giving it the HIGHER
        # generation, and the incoming sits in slot A: recency still decides.
        self.air(self.item_n, 175)
        self.air(self.item_next, 5)
        decks = {
            "A": self.deck_entry("A", self.item_next, generation=4, position=5.0),
            "B": self.deck_entry("B", self.item_n, generation=9, position=175.0),
        }
        resume = make_minimal_stand_in()._build_resume_state(decks, captured_at=0, transport="PLAYING")
        self.assertEqual(resume["log_item_id"], self.item_next.id)

    def test_true_crossfade_end_to_end_resumes_the_winner_and_the_queue_after_it_once(self):
        self.air(self.item_n, 175)
        self.air(self.item_next, 5)
        engine = make_minimal_stand_in()
        engine._engine_session_id = "writer"
        engine.current_log = self.log
        engine.log_items = list(self.log.items.order_by("position"))
        engine._queue_cursor = 2  # past both airing items
        by_id = {item.id: item for item in engine.log_items}
        deck_a = Deck("A", self.track_n, by_id[self.item_n.id], MagicMock(), MagicMock(), generation=1)
        deck_b = Deck("B", self.track_next, by_id[self.item_next.id], MagicMock(), MagicMock(), generation=2)
        deck_a.media_buffer_count = deck_b.media_buffer_count = 120
        engine.decks = {"A": deck_a, "B": deck_b}
        engine._get_deck_position = lambda deck: 175.0 if deck.slot == "A" else 5.0

        engine._write_state(transport="STOPPED")
        fresh = self.engine()
        self.recover(fresh)
        created = self.start_tracks(fresh, 2)

        # N+1 resumes at ITS position; N (outgoing tail) is not restored and
        # N+1 is not replayed from zero; the queue then continues at N+2.
        self.assertEqual(created, [(self.item_next.id, True), (self.item_n2.id, False)])
        self.assertEqual(fresh._queue_cursor, 3)

    def test_resume_position_is_the_winners_saved_position_with_overlap(self):
        self.air(self.item_n, 175)
        self.air(self.item_next, 5)
        self.write_resume_state(item=self.item_next, saved_position=5.0, transport="PLAYING")
        engine = self.engine()
        engine._read_resume_hint()
        self.assertEqual(engine._resume_hint["log_item_id"], self.item_next.id)
        self.assertEqual(engine._resume_hint["position"], 4.75)

    def test_a_dedication_intro_outgoing_does_not_beat_its_incoming_song(self):
        # Pinned judgment call: no Dedications-first exception.  Preferring the
        # outgoing intro would drop the song after only its entrance aired.
        self.air(self.item_n, 20)
        self.air(self.item_next, 3)
        decks = {
            "A": self.deck_entry("A", self.item_n, generation=1, position=20.0, category="Dedications"),
            "B": self.deck_entry("B", self.item_next, generation=2, position=3.0),
        }
        resume = make_minimal_stand_in()._build_resume_state(decks, captured_at=0, transport="PLAYING")
        self.assertEqual(resume["log_item_id"], self.item_next.id)

    def test_an_incoming_dedication_still_wins_over_an_ordinary_outgoing_track(self):
        self.air(self.item_n, 175)
        self.air(self.item_next, 4)
        decks = {
            "A": self.deck_entry("A", self.item_n, generation=1, position=175.0),
            "B": self.deck_entry("B", self.item_next, generation=2, position=4.0, category="Dedications"),
        }
        resume = make_minimal_stand_in()._build_resume_state(decks, captured_at=0, transport="PLAYING")
        self.assertEqual(resume["log_item_id"], self.item_next.id)

    def test_no_deck_actually_playing_means_no_resume_record(self):
        decks = {
            "A": self.deck_entry("A", self.item_n, generation=1, position=1.0, playing=False),
            "B": None,
        }
        self.assertIsNone(make_minimal_stand_in()._build_resume_state(decks, captured_at=0, transport="PLAYING"))


class LegacyR0098UpgradeStateTests(TwoHourFixture):
    """FIX 3 -- the first production upgrade reads r0098's own state file."""

    def legacy_payload(self, *, deck_a, deck_b=None, log_id, queue_cursor, age=0.5):
        return {
            "transport": "STOPPED",
            "timestamp": time.time() - age,
            "engine_session_id": "r0098-session",
            "log_id": log_id,
            "queue_cursor": queue_cursor,
            "decks": {"A": deck_a, "B": deck_b},
        }

    def legacy_deck(self, item, position=4.0):
        return {
            "track_id": item.track_id, "log_item_id": item.id, "position": position,
            "category": self.category.code, "paused": False,
        }

    def test_early_rollover_state_resumes_from_the_items_actual_old_log(self):
        # decks.A.log_item_id -> OLD log; top-level log_id -> the newer queue.
        self.state_path.write_text(json.dumps(self.legacy_payload(
            deck_a=self.legacy_deck(self.item_n), log_id=self.new_log.id, queue_cursor=0,
        )), encoding="utf-8")
        engine = self.engine()

        self.recover(engine)

        self.assertIsNotNone(engine._resume_hint, "a valid early-rollover resume was rejected")
        self.assertEqual(engine._resume_hint["log_item_id"], self.item_n.id)
        self.assertEqual(engine._resume_hint["playlist_log_id"], self.log.id)  # derived from the DB row
        self.assertEqual(engine._resume_hint["queue"]["playlist_log_id"], self.new_log.id)
        self.assertEqual(engine._resume_hint["queue"]["source"], "legacy_r0098")
        self.assertEqual(engine.current_log.id, self.new_log.id)  # queue context kept
        self.assertEqual(engine._resume_item.id, self.item_n.id)
        created = self.start_tracks(engine, 2)
        self.assertEqual([i for i, _ in created], [self.item_n.id, self.x_items[0].id])

    def test_legacy_prepared_successor_in_the_queue_log_returns_to_the_queue(self):
        # r0098 cursor already moved past a PREPARED (unaired) deck item.
        self.state_path.write_text(json.dumps(self.legacy_payload(
            deck_a=self.legacy_deck(self.item_n),
            deck_b=self.legacy_deck(self.x_items[0], position=0.0),
            log_id=self.new_log.id, queue_cursor=1,
        )), encoding="utf-8")
        engine = self.engine()
        self.recover(engine)
        created = self.start_tracks(engine, 2)
        self.assertEqual([i for i, _ in created], [self.item_n.id, self.x_items[0].id])

    def test_legacy_true_crossfade_restores_the_newer_aired_item(self):
        self.item_next.played_at = timezone.now() - timedelta(seconds=2)
        self.item_next.save(update_fields=["played_at"])
        self.state_path.write_text(json.dumps(self.legacy_payload(
            deck_a=self.legacy_deck(self.item_n, position=175.0),
            deck_b=self.legacy_deck(self.item_next, position=2.0),
            log_id=self.log.id, queue_cursor=2,
        )), encoding="utf-8")
        engine = self.engine()
        self.recover(engine)
        self.assertEqual(engine._resume_hint["log_item_id"], self.item_next.id)
        created = self.start_tracks(engine, 2)
        self.assertEqual([i for i, _ in created], [self.item_next.id, self.item_n2.id])

    def test_legacy_successor_that_aired_after_the_snapshot_never_replays(self):
        # r0098 wrote queue_cursor=1 (N+1) and cannot be identity-checked; N+1
        # then aired before the crash.  The loader's first-unplayed cursor wins.
        self.state_path.write_text(json.dumps(self.legacy_payload(
            deck_a=self.legacy_deck(self.item_n), log_id=self.log.id, queue_cursor=1,
        )), encoding="utf-8")
        self.item_next.played_at = timezone.now()
        self.item_next.save(update_fields=["played_at"])
        engine = self.engine()
        self.recover(engine)
        created = self.start_tracks(engine, 2)
        self.assertEqual(created, [(self.item_n.id, True), (self.item_n2.id, False)])

    def test_legacy_same_log_state_still_resumes_exactly(self):
        self.state_path.write_text(json.dumps(self.legacy_payload(
            deck_a=self.legacy_deck(self.item_n), log_id=self.log.id, queue_cursor=1,
        )), encoding="utf-8")
        engine = self.engine()
        self.recover(engine)
        self.assertEqual(engine._resume_hint["playlist_log_id"], self.log.id)
        created = self.start_tracks(engine, 2)
        self.assertEqual([i for i, _ in created], [self.item_n.id, self.item_next.id])

    def test_legacy_fail_closed_checks_are_preserved(self):
        cases = {
            "unapproved old log": lambda: PlaylistLog.objects.filter(id=self.log.id).update(status="draft"),
            "deleted occurrence": lambda: self.item_n.delete(),
            "never aired": lambda: LogItem.objects.filter(id=self.item_n.id).update(played_at=None),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                self.setUp_state_restored = True
                PlaylistLog.objects.filter(id=self.log.id).update(status="approved")
                LogItem.objects.filter(id=self.item_n.id).update(played_at=timezone.now() - timedelta(seconds=4))
                if not LogItem.objects.filter(id=self.item_n.id).exists():
                    self.item_n = self.make_item(
                        17, self.track_n, played_at=timezone.now() - timedelta(seconds=4),
                        scheduled_time=timezone.now(),
                    )
                self.state_path.write_text(json.dumps(self.legacy_payload(
                    deck_a=self.legacy_deck(self.item_n), log_id=self.new_log.id, queue_cursor=0,
                )), encoding="utf-8")
                mutate()
                engine = self.engine()
                engine._read_resume_hint()
                self.assertIsNone(engine._resume_hint)

    def test_legacy_track_identity_mismatch_fails_closed(self):
        deck = self.legacy_deck(self.item_n)
        deck["track_id"] = self.track_next.id
        self.state_path.write_text(json.dumps(self.legacy_payload(
            deck_a=deck, log_id=self.new_log.id, queue_cursor=0,
        )), encoding="utf-8")
        engine = self.engine()
        engine._read_resume_hint()
        self.assertIsNone(engine._resume_hint)

    def test_a_legacy_queue_older_than_the_occurrence_is_ignored(self):
        self.item_next.played_at = timezone.now() - timedelta(seconds=2)
        self.item_next.save(update_fields=["played_at"])
        self.state_path.write_text(json.dumps(self.legacy_payload(
            deck_a=self.legacy_deck(self.x_items[0]), log_id=self.log.id, queue_cursor=2,
        )), encoding="utf-8")
        LogItem.objects.filter(id=self.x_items[0].id).update(played_at=timezone.now() - timedelta(seconds=3))
        engine = self.engine()
        engine._read_resume_hint()
        self.assertIsNone(engine._resume_hint["queue"])


class GatedSeekSnapshotTests(RestartResumeFixture):
    """FIX 4 -- decoded buffers behind a closed seek valve are not program audio."""

    def snapshot(self, *, gated=None, valve_drop=None, transport="STOPPED"):
        engine = make_minimal_stand_in()
        engine._engine_session_id = "writer"
        engine.current_log = self.log
        engine.log_items = list(self.log.items.order_by("position"))
        engine._queue_cursor = 2
        deck = Deck("A", self.track_n, engine.log_items[0], MagicMock(), MagicMock(), generation=5)
        deck.media_buffer_count = 500  # decoded -- but where did it go?
        deck.gated_seek = gated
        if valve_drop is not None:
            deck.seek_gate_valve = MagicMock()
            deck.seek_gate_valve.get_property.side_effect = lambda name: valve_drop if name == "drop" else None
        engine.decks = {"A": deck, "B": None}
        engine._get_deck_position = lambda _deck: 4.0
        engine._write_state(transport=transport)
        return json.loads(self.state_path.read_text(encoding="utf-8")), deck

    def test_unresolved_seek_phases_never_authorize_a_resume_position(self):
        for phase in ("prerolling", "seeking", "confirming"):
            for transport in ("PLAYING", "STOPPED"):  # periodic poll and graceful stop
                with self.subTest(phase=phase, transport=transport):
                    payload, deck = self.snapshot(
                        gated={"phase": phase}, valve_drop=True, transport=transport,
                    )
                    self.assertFalse(payload["decks"]["A"]["actually_playing"])
                    self.assertIsNone(payload["resume"])
                    self.assertFalse(deck.program_gate_open())

    def test_a_closed_valve_is_never_program_even_if_the_seek_record_is_gone(self):
        payload, _deck = self.snapshot(gated=None, valve_drop=True)
        self.assertIsNone(payload["resume"])

    def test_an_unreadable_valve_fails_closed(self):
        engine = make_minimal_stand_in()
        deck = Deck("A", self.track_n, self.item_n, MagicMock(), MagicMock())
        deck.seek_gate_valve = MagicMock()
        deck.seek_gate_valve.get_property.side_effect = RuntimeError("gone")
        self.assertFalse(deck.program_gate_open())

    def test_a_confirmed_and_exposed_seek_generation_is_authoritative(self):
        payload, deck = self.snapshot(gated=None, valve_drop=False)
        self.assertTrue(payload["decks"]["A"]["actually_playing"])
        self.assertEqual(payload["resume"]["log_item_id"], self.item_n.id)
        self.assertTrue(deck.program_gate_open())

    def test_an_ordinary_ungated_deck_is_unaffected(self):
        payload, _deck = self.snapshot()
        self.assertTrue(payload["decks"]["A"]["actually_playing"])
        self.assertIsNotNone(payload["resume"])

    def test_the_valve_opens_before_the_seek_record_is_cleared(self):
        # The "exposed" state may only be reported after the gate truly opened.
        source = __import__("inspect").getsource(PlaybackEngine._resolve_gated_seek)
        self.assertLess(
            source.rindex('op["valve"].set_property("drop", False)'),
            source.rindex("deck.gated_seek = None"),
        )
