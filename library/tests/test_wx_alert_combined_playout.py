"""r0137: engine side of the combined Weather/AMBER announcement (one WxAlert/wx_alert.flac Track).

The real engine queue, insert_urgent synchronization, deck-open authority, hour adoption and restart recovery
run against the database; GStreamer deck construction is the only thing not exercised here (see
test_wx_alert_gstreamer_single_file for the real filesrc behavior).  LIBRARY_ROOT and WEATHER_DATA_DIR are
private temporary directories.
"""
import fcntl
import os
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import override_settings
from django.utils import timezone

import library.services.engine as eng_module
from library.models import Category, LogItem, Track
from library.services import wx_alert_playout
from library.tests import wx_alert_support
from library.tests.cue_marker_support import set_markers
from library.tests.test_engine_restart_resume import TwoHourFixture
from library.tests.test_engine_runtime_commit import make_minimal_stand_in
from library.tests.test_r0135_playout_continuity import _QUEUE_RELATED
from weather.publication import destination_lock_path


class WxAlertPlayoutFixture(TwoHourFixture):
    """Old hour 00:00 (N on air, N+1, N+2 queued) + approved 01:00; the canonical announcement Track."""

    def setUp(self):
        super().setUp()
        root = Path(self.temp_dir.name)
        self.library_root, self.data_dir = root / "music", root / "weather"
        override = override_settings(LIBRARY_ROOT=str(self.library_root), WEATHER_DATA_DIR=str(self.data_dir))
        override.enable()
        self.addCleanup(override.disable)
        self.wx, _ = Category.objects.get_or_create(
            code="WxAlert", defaults={"name": "WxAlert", "kind": self.category.kind})
        self.wx_track = wx_alert_support.make_track(self.library_root, self.wx, self.artist)
        self.events = patch.object(eng_module, "emit_event")
        self.events.start()
        self.addCleanup(self.events.stop)

    # -- coordinator side (what weather_ingest/wx_alert.py does) ----------------------------------
    def announce(self, generation, payload=None, **kwargs):
        time.sleep(0.002)          # strictly after every claim made so far (claims are timezone.now())
        kwargs.setdefault("published_at", timezone.now())
        return wx_alert_support.announce(self.data_dir, self.library_root, payload, generation=generation, **kwargs)

    def deactivate(self, generation):
        wx_alert_support.write_state(self.data_dir, self.library_root, generation=generation, active=False)

    # -- engine --------------------------------------------------------------------------------------
    def queue(self, log):
        return list(log.items.select_related(*_QUEUE_RELATED).order_by("position"))

    def live_engine(self):
        engine = make_minimal_stand_in()
        engine._engine_session_id = "previous-engine-session"
        engine._urgent_retry_counts = {}
        engine._resume_item = None
        engine._resume_hint = None
        engine._poison_skip_identities = []
        engine.current_log = self.log
        engine.log_items = self.queue(self.log)
        engine._queue_cursor = 1                    # N (17) on air; N+1 (18) next
        engine._forced_next_items = []
        engine._create_deck = MagicMock()
        return engine

    def nudge(self, engine):
        """One insert_urgent WxAlert command through the real retrying entry point."""
        with patch.object(eng_module.timezone, "localtime", return_value=timezone.now()), \
                patch.object(eng_module.GLib, "timeout_add_seconds") as retry:
            engine._insert_urgent_next("WxAlert")
        return retry

    def wx_rows(self):
        return list(LogItem.objects.filter(track=self.wx_track).order_by("id"))

    def open_row(self, row):
        """The real final deck-open authority: (track, reason)."""
        with eng_module._deck_open_authority(row) as (track, reason):
            return track, reason

    def claim(self, engine, row):
        self.assertTrue(engine._claim_playback_occurrence(row))
        return LogItem.objects.get(pk=row.pk).playback_claimed_at


class SynchronizationTests(WxAlertPlayoutFixture):
    def test_repeated_nudges_leave_exactly_one_pending_occurrence(self):
        self.announce(1)
        engine = self.live_engine()
        for _ in range(4):
            self.nudge(engine)
        rows = self.wx_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(engine.log_items[engine._queue_cursor].id, rows[0].id)

    def test_changes_before_playback_reuse_the_one_pending_occurrence_with_the_newest_audio(self):
        self.announce(1, b"tornado")
        engine = self.live_engine()
        self.nudge(engine)
        self.announce(2, b"tornado + amber")
        self.nudge(engine)
        self.announce(3, b"amber only")
        self.nudge(engine)
        row, = self.wx_rows()
        item, _forced = engine._next_queue_item()
        self.assertEqual(item.id, row.id)
        track, reason = self.open_row(item)
        self.assertIsNone(reason)
        self.assertEqual(Path(track.filepath).read_bytes(), b"amber only")

    def test_no_occurrence_is_queued_while_inactive_expired_or_unpublished(self):
        engine = self.live_engine()
        self.deactivate(1)
        self.nudge(engine)
        self.announce(2, valid_for=timedelta(seconds=-1))
        self.nudge(engine)
        self.announce(3)
        wx_alert_support.publish_file(self.library_root, b"publication in flight")   # state not written yet
        self.nudge(engine)
        self.assertEqual(self.wx_rows(), [])

    def test_a_claimed_generation_is_never_queued_again_and_a_restart_does_not_repeat_it(self):
        self.announce(1)
        engine = self.live_engine()
        self.nudge(engine)
        row, = self.wx_rows()
        self.claim(engine, row)
        self.nudge(engine)
        fresh = self.live_engine()                     # a restarted engine: no in-memory bookkeeping
        self.nudge(fresh)
        self.assertEqual(len(self.wx_rows()), 1)

    def test_a_change_while_airing_queues_exactly_one_updated_occurrence(self):
        self.announce(1, b"first")
        engine = self.live_engine()
        self.nudge(engine)
        airing = engine._next_queue_item()[0]
        self.assertIsNone(self.open_row(airing)[1])
        self.claim(engine, airing)
        engine.decks["A"] = SimpleNamespace(log_item=airing, track=self.wx_track)
        self.announce(2, b"second")
        self.nudge(engine)
        self.nudge(engine)
        rows = self.wx_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(LogItem.objects.get(pk=airing.pk).playback_claimed_at, airing.playback_claimed_at)
        track, reason = self.open_row(engine._next_queue_item()[0])
        self.assertIsNone(reason)
        self.assertEqual(Path(track.filepath).read_bytes(), b"second")

    def test_content_a_then_b_then_a_is_announced_three_times(self):
        engine = self.live_engine()
        for generation, payload in ((1, b"A"), (2, b"B"), (3, b"A")):
            self.announce(generation, payload)
            self.nudge(engine)
            row = engine._next_queue_item()[0]
            track, reason = self.open_row(row)
            self.assertIsNone(reason, generation)
            self.assertEqual(Path(track.filepath).read_bytes(), payload)
            self.claim(engine, row)
        self.assertEqual(len(self.wx_rows()), 3)

    def test_a_failed_insertion_is_retried_without_duplicates(self):
        self.announce(1)
        engine = self.live_engine()
        with patch.object(type(engine), "_splice_log_item_db_at", side_effect=RuntimeError("db hiccup")):
            retry = self.nudge(engine)
        retry.assert_called_once()
        self.assertEqual(self.wx_rows(), [])
        self.nudge(engine)                          # the scheduled retry (or the next poll's nudge)
        self.nudge(engine)
        self.assertEqual(len(self.wx_rows()), 1)

    def test_a_retired_mp3_track_is_never_selected(self):
        mp3_path = self.library_root / "WxAlert" / "wx_alert.mp3"
        mp3_path.write_bytes(b"ID3 retired")
        mp3 = Track.objects.create(title="Alert", artist=self.artist, category=self.wx, ready2air=True,
                                   filepath=str(mp3_path.resolve()), filename=mp3_path.name)
        self.assertLess(mp3.title, self.wx_track.title)   # first by the category's default ordering
        self.announce(1)
        engine = self.live_engine()
        self.nudge(engine)
        row, = LogItem.objects.filter(category=self.wx)
        self.assertEqual(row.track_id, self.wx_track.id)
        self.assertEqual(wx_alert_playout.audio_track().id, self.wx_track.id)

    def test_other_urgent_categories_keep_the_unchanged_insert_path(self):
        pa, _ = Category.objects.get_or_create(code="UrgentPA", defaults={"name": "UrgentPA", "kind": self.category.kind})
        pa_track = self.make_track("Urgent PA")
        Track.objects.filter(pk=pa_track.pk).update(category=pa)
        engine = self.live_engine()
        with patch.object(eng_module.timezone, "localtime", return_value=timezone.now()):
            engine._insert_urgent_next_inner("UrgentPA")
            engine._insert_urgent_next_inner("UrgentPA")
        self.assertEqual(LogItem.objects.filter(track=pa_track).count(), 2)   # no idempotence imposed


class DeckOpenAuthorityTests(WxAlertPlayoutFixture):
    def pending(self, generation=1, payload=None):
        self.announce(generation, payload)
        engine = self.live_engine()
        self.nudge(engine)
        return engine, engine.log_items[engine._queue_cursor]

    def test_inactive_or_expired_announcements_never_open(self):
        engine, row = self.pending()
        self.deactivate(2)
        self.assertIn("no Weather/AMBER alert is active", self.open_row(row)[1])
        wx_alert_support.write_state(self.data_dir, self.library_root, generation=3, valid_for=timedelta(seconds=-1))
        self.assertIn("expired", self.open_row(row)[1])
        (self.data_dir / "wx_alert_state.json").unlink()
        self.assertIn("missing", self.open_row(row)[1])

    def test_a_publication_in_progress_refuses_without_blocking(self):
        engine, row = self.pending()
        lock_path = destination_lock_path(wx_alert_playout.audio_path())
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)                 # what publish_weather_asset holds
            track, reason = self.open_row(row)
        finally:
            os.close(fd)
        self.assertIsNone(track)
        self.assertIn("publication is in progress", reason)
        self.assertIsNone(self.open_row(row)[1])

    def test_the_shared_lock_is_held_until_the_deck_open_scope_ends(self):
        engine, row = self.pending()
        lock_path = destination_lock_path(wx_alert_playout.audio_path())
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        with eng_module._deck_open_authority(row) as (track, reason):
            self.assertIsNone(reason)
            with self.assertRaises(BlockingIOError):     # a publication cannot replace the file now
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # ...and can once the file has been opened
        fcntl.flock(fd, fcntl.LOCK_UN)

    def test_a_published_file_not_described_by_the_state_never_opens(self):
        engine, row = self.pending()
        wx_alert_support.publish_file(self.library_root, b"next generation, state not yet recorded")
        self.assertIn("publication pending", self.open_row(row)[1])

    def test_a_forced_occurrence_opens_with_the_refreshed_track_timing(self):
        engine, row = self.pending(1, b"short")
        engine.log_items.remove(row)
        engine._forced_next_items = [row]                 # forced-list objects are never reloaded
        self.assertEqual(row.track.duration_seconds, 30.0)
        Track.objects.filter(pk=self.wx_track.pk).update(duration_seconds=95.0)   # what the publication analyzed
        set_markers(self.wx_track.pk, next_start_seconds=93.5)
        self.announce(2, b"much longer combined announcement")
        item, forced = engine._next_queue_item()
        track, reason = self.open_row(item)
        self.assertIsNone(reason)
        self.assertEqual((track.duration_seconds, track.next_start_seconds), (95.0, 93.5))
        self.assertIs(item.track, track)                  # what _create_deck hands to the Deck

    def test_a_second_occurrence_of_an_already_claimed_generation_never_opens(self):
        engine, row = self.pending()
        duplicate = engine._splice_log_item_db_at(engine._queue_cursor + 1, self.wx_track, self.wx)  # legacy duplicate
        self.claim(engine, row)
        self.assertIn("already claimed", self.open_row(duplicate)[1])

    def test_a_recreated_occurrence_never_continues_into_a_newer_generation(self):
        engine, row = self.pending(1, b"first")
        self.claim(engine, row)
        self.assertIsNone(self.open_row(row)[1])          # same generation: a resume/seek may reopen it
        self.announce(2, b"second")
        self.assertIn("earlier announcement", self.open_row(row)[1])

    def test_a_retired_mp3_occurrence_never_opens(self):
        mp3_path = self.library_root / "WxAlert" / "wx_alert.mp3"
        mp3_path.write_bytes(b"ID3 retired")
        mp3 = Track.objects.create(title="Alert", artist=self.artist, category=self.wx, ready2air=True,
                                   filepath=str(mp3_path.resolve()), filename=mp3_path.name)
        self.announce(1)
        row = LogItem.objects.create(playlist_log=self.log, position=99, scheduled_time=timezone.now(),
                                     track=mp3, track_title="Alert", track_artist="", category=self.wx,
                                     origin=LogItem.ORIGIN_ENGINE_INSERTED)
        self.assertIn("not the canonical announcement Track", self.open_row(row)[1])

    def test_an_ordinary_track_needs_no_alert_state(self):
        (self.data_dir / "wx_alert_state.json").unlink(missing_ok=True)
        row = self.queue(self.log)[1]
        track, reason = self.open_row(row)
        self.assertIsNone(reason)
        self.assertEqual(track.id, self.track_next.id)


class HourRolloverTests(WxAlertPlayoutFixture):
    def adopt(self, engine):
        now = timezone.make_aware(timezone.datetime(2026, 10, 1, 0, 59, 36))
        with patch.object(eng_module.timezone, "localtime", return_value=now):
            engine._advance_to_next_hour_log(self.new_log.date, 1)

    def test_early_adoption_carries_the_pending_occurrence_into_forced_next(self):
        self.announce(1)
        engine = self.live_engine()
        self.nudge(engine)
        row, = self.wx_rows()
        self.adopt(engine)
        self.assertEqual(engine.current_log.id, self.new_log.id)
        self.assertEqual([i.id for i in engine._forced_next_items], [row.id])
        self.assertEqual(engine._queue_cursor, 0)
        walked = [engine._next_queue_item()[0].id for _ in range(3)]
        self.assertEqual(walked, [row.id, self.x_items[0].id, self.x_items[1].id])
        self.assertEqual(engine._forced_next_items, [])
        self.assertIsNone(self.open_row(LogItem.objects.get(pk=row.pk))[1])

    def test_a_second_adoption_or_nudge_never_duplicates_a_carried_occurrence(self):
        self.announce(1)
        engine = self.live_engine()
        self.nudge(engine)
        self.adopt(engine)
        self.adopt(engine)                                 # idempotent advance
        self.nudge(engine)                                 # the carried row is still the pending one
        self.assertEqual(len(engine._forced_next_items), 1)
        self.assertEqual(len(self.wx_rows()), 1)

    def test_a_carried_occurrence_goes_ahead_of_a_pending_dedication_follow_up(self):
        self.announce(1)
        engine = self.live_engine()
        follow_up = engine.log_items[2]
        engine._forced_next_items = [follow_up]
        self.nudge(engine)                                 # inserted ahead of the follow-up (existing policy)
        wx_row, = self.wx_rows()
        self.assertEqual([i.id for i in engine._forced_next_items], [wx_row.id, follow_up.id])
        self.adopt(engine)
        self.assertEqual([i.id for i in engine._forced_next_items], [wx_row.id, follow_up.id])

    def test_claimed_or_aired_occurrences_are_not_carried(self):
        self.announce(1)
        engine = self.live_engine()
        self.nudge(engine)
        row, = self.wx_rows()
        self.claim(engine, engine.log_items[engine._queue_cursor])
        engine.log_items = self.queue(self.log)            # the 3 s reload picks up the claim
        self.adopt(engine)
        self.assertEqual(engine._forced_next_items, [])

    def test_a_carried_occurrence_cancelled_before_the_boundary_does_not_play(self):
        self.announce(1)
        engine = self.live_engine()
        self.nudge(engine)
        self.adopt(engine)
        self.deactivate(2)
        row = engine._next_queue_item()[0]
        self.assertIn("no Weather/AMBER alert is active", self.open_row(row)[1])

    def test_exhausted_queue_rollover_strands_nothing(self):
        self.announce(1)
        engine = self.live_engine()
        self.nudge(engine)
        engine._queue_cursor = len(engine.log_items)       # everything ahead was consumed
        engine._ensure_dst_fallback_replay = lambda reason: False
        engine._peek_next_hour = lambda: (self.new_log, self.queue(self.new_log))
        engine._try_extend_live_log_async = MagicMock()
        self.assertTrue(engine._roll_over_to_next_hour())
        self.assertEqual(engine._forced_next_items, [])


class RestartTests(WxAlertPlayoutFixture):
    def test_restart_restores_a_valid_pending_occurrence_and_refuses_a_cancelled_one(self):
        self.announce(1)
        engine = self.live_engine()
        self.nudge(engine)
        row, = self.wx_rows()
        fresh = self.engine()
        self.recover(fresh)
        nxt = fresh._next_queue_item()[0]
        self.assertEqual(nxt.id, row.id)
        self.assertIsNone(self.open_row(nxt)[1])
        self.deactivate(2)
        self.assertIsNotNone(self.open_row(nxt)[1])
