"""r0135 P0 -- KOGR 2026-11-11 playout continuity (dead air + overlapping rollover starts).

Failure A: the legacy live-fill worker copied every in-memory LogItem into its
historical picks. After the operator deleted three Weather Tracks, the ~3 s
queue reload materialized those rows with ``track=None`` (on_delete=SET_NULL),
and ``fill_remaining_hour`` dereferenced ``None.id`` -- every dispatch crashed,
no PSA was ever proposed, and the hour ran ~175 s silent.

Failure B: while the queue was exhausted, every 10 s scheduler tick's idle
recovery re-ran ``_start_next_track`` -> ``_on_log_exhausted``, which armed a
NEW perpetual 30 s ``_try_load_next_hour`` chain each time (one per tick, all
surviving). When the 22:00 log was installed ahead of the hour, those stale
chains reloaded the wall-clock 21:00 log (moving the queue backward), rolled
over again and started further next-hour items on the free deck while the
first was still playing.

Both scenarios use the real engine methods named in each test; only GStreamer
deck creation, the GLib main loop/clock and request/dedication side effects are
replaced by deterministic stand-ins. Nothing here writes a PlayEvent.
"""
import tempfile
import threading
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import library.services.engine as eng_module
from library.models import (
    Artist, Category, CategoryKind, LogFillConfig, LogItem, PlaylistLog, Rotation, ScheduleBlock, Track,
)
from library.services.engine import Deck
from library.services.log_builder import effective_airtime_seconds, fill_remaining_hour
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.tests.test_engine_restart_resume import TwoHourFixture   # fixture only: it defines no tests
from library.tests.test_engine_runtime_commit import make_minimal_stand_in
from monitoring.models import SystemEvent


WEDNESDAY = date(2027, 3, 3)          # no DST transition near this date


def at(hour, minute, second=0, micro=0, day=WEDNESDAY):
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour, minute, second, micro))


class _Fixture:
    """Library rows shared by both failure families."""

    def build_library(self):
        ensure_schedule_profile_state()
        spot, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
        music, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.psa = Category.objects.create(code="R135PSA", name="R135 PSA", kind=spot)
        self.blues = Category.objects.create(code="R135BLUES", name="R135 Blues", kind=music)
        self.weather = Category.objects.create(code="R135WX", name="R135 Weather", kind=spot)
        self.psa_tracks = []
        for i, seconds in enumerate((14.0, 20.0, 25.0, 29.0, 30.0, 31.0, 45.0, 58.0, 59.0)):
            artist, _ = Artist.get_or_create_ci(f"R135 PSA Sponsor {i}")
            self.psa_tracks.append(Track.objects.create(
                filepath=f"/srv/isadoraair/r0135/psa{i}.flac", filename=f"psa{i}.flac", title=f"PSA {i}",
                artist=artist, category=self.psa, ready2air=True,
                duration_seconds=seconds, next_start_seconds=seconds,
            ))
        self.music_tracks = []
        for i in range(8):
            artist, _ = Artist.get_or_create_ci(f"R135 Blues Artist {i}")
            self.music_tracks.append(Track.objects.create(
                filepath=f"/srv/isadoraair/r0135/blues{i}.flac", filename=f"blues{i}.flac", title=f"Blues {i}",
                artist=artist, category=self.blues, ready2air=True,
                duration_seconds=200.0, next_start_seconds=198.0,
            ))
        LogFillConfig.objects.update_or_create(
            pk=1, defaults={"strategy": "fixed_category", "fallback_category": self.psa},
        )

    def make_log(self, hour, rows, *, played=True, day=WEDNESDAY, minute=0):
        """rows: list of (track_or_None, category). Track None == a deleted Track (SET_NULL)."""
        log = PlaylistLog.objects.create(date=day, hour=hour, status="approved")
        for position, (track, category) in enumerate(rows):
            LogItem.objects.create(
                playlist_log=log, position=position,
                scheduled_time=at(hour, minute, day=day) + timedelta(seconds=60 * position),
                track=track, track_title=track.title if track else "Deleted Weather",
                category=category, played_at=timezone.now() if played else None,
            )
        return log

    @staticmethod
    def reload_items(log):
        """Exactly the engine's ~3 s queue reload materialization."""
        return list(log.items.select_related(
            "track", "track__artist", "track__album", "track__category", "track__category__kind",
            "category", "category__kind",
        ).order_by("position"))


def _live_fill_engine():
    engine = object.__new__(eng_module.PlaybackEngine)
    engine.running = True
    engine.decks = {"A": None, "B": SimpleNamespace(paused=False, finished=False)}   # final song playing
    engine.manual_mode = False
    engine._lock = threading.RLock()
    engine._live_fill_in_progress = False
    engine._live_fill_generation = 0
    engine._last_live_extend_attempt = 0.0
    engine._refill_coordinator = None
    engine._start_next_track = MagicMock()
    return engine


class _SyncThread:
    """Runs the worker inline so the real worker + fill_remaining_hour execute deterministically."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class LegacyLiveFillNullHistoryTests(_Fixture, TransactionTestCase):
    """Failure A -- dispatch -> real worker -> real fill_remaining_hour -> real install."""

    def setUp(self):
        super().setUp()
        self.build_library()

    def incident_log(self, null_positions=(1, 3, 5), total=8):
        rows = []
        for position in range(total):
            if position in null_positions:
                rows.append((None, self.weather))
            else:
                rows.append((self.music_tracks[position % len(self.music_tracks)], self.blues))
        return self.make_log(21, rows)

    def dispatch_and_install(self, log, *, deficit=208.466, dispatch_at=at(21, 54, 55), install_at=at(21, 54, 56)):
        engine = _live_fill_engine()
        engine.current_log = log
        engine.log_items = self.reload_items(log)
        engine._queue_cursor = len(engine.log_items)
        seconds_left = 3600 - (dispatch_at.minute * 60 + dispatch_at.second)
        engine._committed_future_runway_seconds = MagicMock(return_value=seconds_left - deficit)
        engine._apply_poison_skip = lambda items: items
        idle_calls = []
        with patch.object(eng_module.timezone, "localtime", return_value=dispatch_at), \
                patch.object(eng_module.threading, "Thread", _SyncThread), \
                patch.object(eng_module.GLib, "idle_add", side_effect=lambda fn, *a: idle_calls.append((fn, a)) or 1):
            engine._try_extend_live_log_async()
        crashes = list(SystemEvent.objects.filter(title="Live-fill worker crashed").values_list("detail", flat=True))
        installed = []
        if idle_calls:
            fn, args = idle_calls[0]
            with patch.object(eng_module.timezone, "localtime", return_value=install_at):
                fn(*args)
            installed = list(LogItem.objects.filter(playlist_log=log, origin=LogItem.ORIGIN_LIVE_FILL).order_by("position"))
        return engine, idle_calls, crashes, installed

    def assert_psa_fill(self, log, engine, idle_calls, crashes, installed, *, prior):
        self.assertEqual(crashes, [], "live-fill worker crashed: %s" % (crashes and crashes[0].get("traceback")))
        self.assertEqual(len(idle_calls), 1, "no proposal was handed to the GLib install callback")
        _fn, (log_id, generation, new_picks, expected_prior_count, start_position) = idle_calls[0]
        self.assertEqual((log_id, generation), (log.id, engine._live_fill_generation))
        self.assertEqual(expected_prior_count, prior)               # the queue length incl. NULL rows
        self.assertEqual(start_position, prior)                     # appended after the last existing row
        self.assertTrue(new_picks)
        self.assertTrue(all(p["track"] is not None and p["track"].category_id == self.psa.id for p in new_picks))
        fill = sum(effective_airtime_seconds(p["track"]) for p in new_picks)
        self.assertLessEqual(fill, 208.466 + eng_module.DURATION_FIT_MARGIN)
        self.assertGreaterEqual(fill, 208.466 - eng_module.DURATION_FIT_MARGIN)
        self.assertEqual([i.position for i in installed], list(range(prior, prior + len(new_picks))))
        self.assertTrue(all(i.category_id == self.psa.id and i.track_id for i in installed))
        self.assertEqual(len(engine.log_items), prior + len(installed))
        self.assertEqual([i.id for i in engine.log_items[prior:]], [i.id for i in installed])
        engine._start_next_track.assert_not_called()                # the final song still owns its deck
        self.assertFalse(engine._live_fill_in_progress)

    def test_incident_three_deleted_weather_tracks_still_fill_the_deficit_with_psas(self):
        log = self.incident_log()
        engine, idle_calls, crashes, installed = self.dispatch_and_install(log)
        self.assert_psa_fill(log, engine, idle_calls, crashes, installed, prior=8)
        # Invalid historical rows stay unplayable rows; nothing re-airs them.
        self.assertEqual(LogItem.objects.filter(playlist_log=log, track__isnull=True).count(), 3)

    def test_one_null_history_row(self):
        log = self.incident_log(null_positions=(2,))
        self.assert_psa_fill(log, *self.dispatch_and_install(log), prior=8)

    def test_null_history_row_at_the_final_position(self):
        log = self.incident_log(null_positions=(7,))
        self.assert_psa_fill(log, *self.dispatch_and_install(log), prior=8)

    def test_no_valid_history_rows_at_all(self):
        log = self.incident_log(null_positions=tuple(range(4)), total=4)
        self.assert_psa_fill(log, *self.dispatch_and_install(log), prior=4)

    def test_fully_valid_history_is_unchanged(self):
        log = self.incident_log(null_positions=())
        self.assert_psa_fill(log, *self.dispatch_and_install(log), prior=8)

    def test_valid_history_keeps_its_separation_influence(self):
        # A PSA already in this hour (valid row) is hard-excluded while
        # alternatives exist, alongside NULL rows that contribute nothing.
        rows = [(None, self.weather), (self.psa_tracks[3], self.psa), (None, self.weather), (self.music_tracks[0], self.blues)]
        history = [{"track": li.track, "category": li.category} for li in self.reload_items(self.make_log(21, rows))]
        before = list(history)                                       # fill_remaining_hour extends in place
        picks, _ = fill_remaining_hour(history, 0.0, at(21, 0), target_duration_seconds=208.466)
        new = picks[len(before):]
        self.assertTrue(new)
        self.assertNotIn(self.psa_tracks[3].id, [p["track"].id for p in new])
        self.assertEqual(picks[:len(before)], before)               # history untouched, NULLs included

    def test_no_configured_fallback_category_proposes_nothing_without_crashing(self):
        LogFillConfig.objects.update_or_create(pk=1, defaults={"strategy": "fixed_category", "fallback_category": None})
        log = self.incident_log()
        engine, idle_calls, crashes, installed = self.dispatch_and_install(log)
        self.assertEqual((crashes, idle_calls, installed), ([], [], []))
        self.assertFalse(engine._live_fill_in_progress)

    def test_fallback_category_without_eligible_assets_proposes_nothing_without_crashing(self):
        Track.objects.filter(category=self.psa).update(ready2air=False)
        log = self.incident_log()
        engine, idle_calls, crashes, installed = self.dispatch_and_install(log)
        self.assertEqual((crashes, idle_calls, installed), ([], [], []))
        self.assertFalse(engine._live_fill_in_progress)

    def test_repeat_last_category_strategy_with_a_null_last_row(self):
        LogFillConfig.objects.update_or_create(pk=1, defaults={"strategy": "repeat_last_category", "fallback_category": None})
        rows = [(self.music_tracks[0], self.blues), (None, self.psa)]
        history = [{"track": li.track, "category": li.category} for li in self.reload_items(self.make_log(21, rows))]
        picks, _ = fill_remaining_hour(history, 0.0, at(21, 0), target_duration_seconds=120.0)
        self.assertTrue(all(p["track"].category_id == self.psa.id for p in picks[2:]))
        self.assertTrue(picks[2:])

    def test_stale_proposal_after_queue_change_is_still_discarded(self):
        # The NULL-tolerant history must not weaken the expected-length check.
        log = self.incident_log()
        engine = _live_fill_engine()
        engine.current_log = log
        engine.log_items = self.reload_items(log)
        engine._committed_future_runway_seconds = MagicMock(return_value=96.5)
        idle_calls = []
        with patch.object(eng_module.timezone, "localtime", return_value=at(21, 54, 55)), \
                patch.object(eng_module.threading, "Thread", _SyncThread), \
                patch.object(eng_module.GLib, "idle_add", side_effect=lambda fn, *a: idle_calls.append((fn, a)) or 1):
            engine._try_extend_live_log_async()
        self.assertEqual(len(idle_calls), 1)
        engine.log_items = engine.log_items[:-1]                   # queue changed since dispatch
        fn, args = idle_calls[0]
        with patch.object(eng_module.timezone, "localtime", return_value=at(21, 54, 56)):
            fn(*args)
        self.assertFalse(LogItem.objects.filter(playlist_log=log, origin=LogItem.ORIGIN_LIVE_FILL).exists())


# --------------------------------------------------------------------------- Failure B

class _Source:
    def __init__(self, source_id):
        self._id = source_id

    def get_id(self):
        return self._id


class FakeGLib:
    """Deterministic GLib timer/idle queue on the simulation clock."""

    def __init__(self, sim):
        self.sim = sim
        self.timers = {}
        self.next_id = 1000
        self.current = None

    def timeout_add_seconds(self, seconds, fn, *args):
        return self._add(seconds, fn, args)

    def timeout_add(self, ms, fn, *args):
        return self._add(ms / 1000.0, fn, args)

    def _add(self, seconds, fn, args):
        self.next_id += 1
        self.timers[self.next_id] = [self.sim.now + timedelta(seconds=seconds), seconds, fn, args]
        return self.next_id

    def idle_add(self, fn, *args):
        self.sim.idle.append((fn, args))
        return 1

    def source_remove(self, source_id):
        return self.timers.pop(source_id, None) is not None

    def main_current_source(self):
        return self.current

    def retry_timers(self, engine):
        return [sid for sid, (_due, _s, fn, _a) in self.timers.items()
                if getattr(fn, "__name__", "") == "_try_load_next_hour"]


class RunawayRecursion(BaseException):
    """Raised by the instrumentation (BaseException: _glib_safe must not swallow it)."""


class RolloverSim:
    """Discrete-event driver: deck EOS, scheduler ticks, GLib timers, scripted build install."""

    def __init__(self, test, engine, start):
        self.test, self.engine, self.now = test, engine, start
        self.glib = FakeGLib(self)
        self.idle = []
        self.starts = []            # (time, slot, item id, title, authority, other deck live)
        self.max_retry_chains = 0
        self.log_hours = []
        self.authority = None
        self.scripted = []
        self.poll = None
        self.runaway = []           # (time, authority, nested reloads) for every bounded-recursion trip
        self.loads = 0
        self.call_loads = 0
        self.exhaust_depth = 0
        self.max_exhaust_depth = 0
        self.timer_firings = 0
        self.next_tick = start + timedelta(seconds=4)

    def fake_create_deck(self, slot, log_item):
        engine = self.engine
        other = engine.decks["B" if slot == "A" else "A"]
        LogItem.objects.filter(id=log_item.id).update(played_at=timezone.now(), playback_claimed_at=timezone.now())
        log_item.played_at = timezone.now()
        seconds = effective_airtime_seconds(log_item.track)
        deck = SimpleNamespace(log_item=log_item, track=log_item.track, paused=False, finished=False,
                               ends_at=self.now + timedelta(seconds=seconds))
        engine.decks[slot] = deck
        self.starts.append((self.now, slot, log_item.id, log_item.track.title, self.authority, other is not None))
        return deck

    def instrument(self, engine):
        load, exhausted = engine._load_log_for, engine._on_log_exhausted

        def counted_load(*args):
            self.loads += 1
            self.call_loads += 1
            if self.call_loads > 12:
                raise RunawayRecursion(self.call_loads)
            return load(*args)

        def nested_exhausted(*args):
            self.exhaust_depth += 1
            self.max_exhaust_depth = max(self.max_exhaust_depth, self.exhaust_depth)
            try:
                return exhausted(*args)
            finally:
                self.exhaust_depth -= 1
        engine._load_log_for = counted_load
        engine._on_log_exhausted = nested_exhausted

    def call(self, authority, fn, *args):
        self.authority = authority
        self.call_loads = 0
        try:
            return fn(*args)
        except RunawayRecursion as exc:
            self.runaway.append((self.now, authority, exc.args[0]))
            return False
        finally:
            self.authority = None
            if self.engine.current_log is not None:
                self.log_hours.append((self.now, self.engine.current_log.hour))

    def run_until(self, end):
        engine = self.engine
        step = timedelta(milliseconds=250)
        while self.now < end:
            self.now += step
            for slot in ("A", "B"):
                deck = engine.decks[slot]
                if deck is not None and deck.ends_at <= self.now:
                    engine.decks[slot] = None
                    if engine.decks["A"] is None and engine.decks["B"] is None and not engine.manual_mode:
                        self.call("eos", engine._start_next_track)
            for when, fn in list(self.scripted):
                if when <= self.now:
                    self.scripted.remove((when, fn))
                    fn()
            while self.idle:
                fn, args = self.idle.pop(0)
                self.call("idle", fn, *args)
            for sid in sorted(self.glib.timers):
                entry = self.glib.timers.get(sid)
                if entry is None or entry[0] > self.now:
                    continue
                _due, interval, fn, args = entry
                self.glib.current = _Source(sid)
                self.timer_firings += 1
                try:
                    keep = self.call(f"timer:{getattr(fn, '__name__', fn)}", fn, *args)
                finally:
                    self.glib.current = None
                if keep and sid in self.glib.timers:
                    self.glib.timers[sid][0] = self.now + timedelta(seconds=interval)
                else:
                    self.glib.timers.pop(sid, None)
            if self.poll is not None:
                self.call("poll", self.poll)
            if self.now >= self.next_tick:
                self.next_tick += timedelta(seconds=eng_module.AUTO_BUILD_CHECK_SECONDS)
                self.call("tick", engine._ensure_upcoming_logs)
            self.max_retry_chains = max(self.max_retry_chains, len(self.glib.retry_timers(engine)))


def _rollover_engine(sim):
    engine = object.__new__(eng_module.PlaybackEngine)
    engine.running = True
    engine.decks = {"A": None, "B": None}
    engine.manual_mode = False
    engine._lock = threading.RLock()
    engine._building_hours = set()
    engine.current_log = None
    engine.log_items = []
    engine._queue_cursor = 0
    engine._next_hour_peek = None
    engine._next_hour_peek_at = 0.0
    engine._next_hour_peek_key = None
    engine._last_live_extend_attempt = 0.0
    engine._live_fill_in_progress = False
    engine._live_fill_generation = 0
    engine._forced_next_items = []
    engine._poison_skip_identities = []
    engine._resume_hint = None
    engine._active_message_held = {}
    engine._next_triggered = False
    engine._manual_from_mic = False
    engine._manual_hold_pending = False
    engine._hour_refill_evidence = None
    engine._refill_coordinator = None
    engine._try_extend_live_log_async = MagicMock(return_value=False)
    engine._project_upcoming_hour_target_duration = MagicMock(return_value=eng_module.NOMINAL_HOUR_SECONDS)
    engine._ensure_log_building = MagicMock(return_value="building")
    engine._deck_slot_available = lambda slot: True
    engine._maybe_insert_dedication_intro = lambda item: item
    engine._offer_automatic_skip_evidence = MagicMock()
    engine._note_active_message_discard = MagicMock()
    engine._create_deck = sim.fake_create_deck
    return engine


class _RolloverFixture(_Fixture):
    """Real retry/tick/install/start authorities on a simulated clock."""

    def setUp(self):
        super().setUp()
        self.build_library()
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.media = media.name
        for track in Track.objects.filter(category__in=(self.blues, self.psa)):
            path = f"{media.name}/{track.filename}"
            open(path, "wb").close()
            Track.objects.filter(id=track.id).update(filepath=path)
        self.music_tracks = list(Track.objects.filter(category=self.blues).order_by("id"))
        self.psa_tracks = list(Track.objects.filter(category=self.psa).order_by("id"))
        profile = ensure_schedule_profile_state().active_profile
        rotation = Rotation.objects.create(name="R135 Rotation")
        for hour in (21, 22):
            ScheduleBlock.objects.create(profile=profile, specific_date=WEDNESDAY, start_time=dt_time(hour, 0),
                                         end_time=dt_time((hour + 1) % 24, 0), rotation=rotation)
        self.old_log = self.make_log(21, [(t, self.blues) for t in self.music_tracks[:4]])
        self.gd = Category.objects.create(code="R135GD", name="R135 GDead", kind=self.blues.kind)
        self.next_rows = []
        for title, seconds, category in (("Station ID", 3.0, self.weather), ("1990 Intro", 54.0, self.gd),
                                         ("Calendar of Events", 22.0, self.weather), ("Promo", 30.0, self.psa),
                                         ("Underwriter", 21.0, self.psa), ("1990 Part 1", 600.0, self.gd)):
            artist, _ = Artist.get_or_create_ci(f"R135 {title}")
            path = f"{media.name}/{title}.flac"
            open(path, "wb").close()
            self.next_rows.append((Track.objects.create(
                filepath=path, filename=f"{title}.flac", title=title,
                artist=artist, category=category, ready2air=True,
                duration_seconds=seconds, next_start_seconds=seconds), category))

    def simulate(self, *, approve_at=at(21, 59, 34), end=at(22, 0, 40), last_song_ends=at(21, 56, 39),
                 old_log=None, real_live_fill=False, start=at(21, 54, 49), next_rows=None, next_hour=22,
                 next_minute=0, events=(), setup=None):
        sim = RolloverSim(self, None, start)
        engine = _rollover_engine(sim)
        sim.engine = engine
        sim.instrument(engine)
        if old_log is not None:
            self.old_log = old_log
        if real_live_fill:
            del engine._try_extend_live_log_async          # the real dispatcher
            engine._apply_poison_skip = lambda items: items

            def committed_runway():
                live = sum((d.ends_at - sim.now).total_seconds() for d in engine.decks.values() if d is not None)
                future = sum(effective_airtime_seconds(i.track) for i in engine.log_items[engine._queue_cursor:]
                             if i.track is not None)
                return max(0.0, live) + future
            engine._committed_future_runway_seconds = committed_runway
            sim.poll = lambda: (engine._queue_cursor >= len(engine.log_items) and engine.current_log is not None
                                and engine.current_log.hour == 21 and engine._try_extend_live_log_async())
        engine.current_log = self.old_log
        engine.log_items = self.reload_items(self.old_log)
        engine._queue_cursor = len(engine.log_items)
        last = engine.log_items[-1]
        if last_song_ends is not None:
            engine.decks["B"] = SimpleNamespace(log_item=last, track=last.track, paused=False, finished=False,
                                                ends_at=last_song_ends)
        rows = self.next_rows if next_rows is None else next_rows

        def approve_next_hour():
            self.next_log = self.make_log(next_hour, rows, played=False, minute=next_minute)
            sim.call("install", engine._install_built_hour, WEDNESDAY, next_hour)

        if approve_at is not None:
            sim.scripted.append((approve_at, approve_next_hour))
        for when, fn in events:
            sim.scripted.append((when, lambda fn=fn: fn(sim, engine)))
        if setup is not None:
            setup(sim, engine)
        real_localtime = timezone.localtime

        def localtime(value=None, *args, **kwargs):
            return real_localtime(value, *args, **kwargs) if value is not None else sim.now

        with patch.object(eng_module, "GLib", sim.glib), \
                patch.object(eng_module.timezone, "localtime", side_effect=localtime), \
                patch.object(eng_module, "SCHEDULE_AUTHORITY_REVALIDATE_SECONDS", 0), \
                patch.object(eng_module, "maybe_schedule_song_request", return_value=None), \
                patch.object(eng_module, "emit_event") as emitted, \
                patch.object(eng_module.threading, "Thread", _SyncThread):
            sim.run_until(end)
        sim.emitted = [c.kwargs for c in emitted.call_args_list]
        return sim


class ExhaustedLogRecoveryOwnershipTests(_RolloverFixture, TransactionTestCase):
    """Failure B."""

    def test_incident_rollover_has_one_retry_owner_and_no_unowned_starts(self):
        sim = self.simulate()
        report = "\n".join(f"{t:%H:%M:%S.%f} {slot} {title!r} by {who} other_live={o}"
                           for t, slot, _id, title, who, o in sim.starts)
        self.assertLessEqual(sim.max_retry_chains, 1, f"retry chains multiplied to {sim.max_retry_chains}:\n" + report)
        unowned = [s for s in sim.starts if s[4].startswith("timer:") and s[5]]
        self.assertEqual(unowned, [], "a retry started an item while another deck was live:\n" + report)
        titles = [s[3] for s in sim.starts]
        self.assertEqual(titles, ["Station ID", "1990 Intro", "Calendar of Events"], report)
        self.assertEqual([s[4] for s in sim.starts], ["install", "eos", "eos"], report)
        # Never two program items on air at once in this (crossfade-free) scenario.
        self.assertTrue(all(not s[5] for s in sim.starts), report)
        # Once the 22:00 queue is installed the active queue never moves back to 21:00.
        first_22 = next(t for t, hour in sim.log_hours if hour == 22)
        self.assertEqual([h for t, h in sim.log_hours if t >= first_22 and h != 22], [])
        # No retry chain survives successful recovery.
        self.assertEqual(sim.glib.retry_timers(sim.engine), [])

    def test_retries_before_approval_keep_exactly_one_pending_chain(self):
        sim = self.simulate(approve_at=at(23, 0), end=at(21, 59, 30))
        self.assertEqual(len(sim.glib.retry_timers(sim.engine)), 1)
        self.assertLessEqual(sim.max_retry_chains, 1)
        self.assertEqual([s[3] for s in sim.starts], [])
        self.assertTrue(all(hour == 21 for _t, hour in sim.log_hours))

    def test_approval_just_before_and_after_a_retry_firing_starts_the_hour_once(self):
        for approve_at in (at(21, 59, 8, 750000), at(21, 59, 9, 250000), at(21, 59, 38, 900000), at(21, 59, 39, 100000)):
            with self.subTest(approve_at=approve_at):
                LogItem.objects.filter(playlist_log__hour=22).delete()
                PlaylistLog.objects.filter(hour=22).delete()
                LogItem.objects.filter(playlist_log=self.old_log).update(played_at=timezone.now())
                sim = self.simulate(approve_at=approve_at, end=approve_at + timedelta(seconds=70))
                self.assertLessEqual(sim.max_retry_chains, 1)
                self.assertEqual([s[3] for s in sim.starts][:3], ["Station ID", "1990 Intro", "Calendar of Events"])
                self.assertTrue(all(not s[5] for s in sim.starts))
                self.assertEqual(sim.glib.retry_timers(sim.engine), [])

    def test_teardown_with_a_pending_retry_leaves_no_live_authority(self):
        sim = self.simulate(approve_at=at(23, 0), end=at(21, 57, 30))
        self.assertEqual(len(sim.glib.retry_timers(sim.engine)), 1)
        engine = sim.engine
        with patch.object(eng_module, "GLib", sim.glib):
            engine.running = False
            engine._cancel_exhausted_retry()
        self.assertEqual(sim.glib.retry_timers(engine), [])
        # A stale firing that GLib already dispatched is still inert.
        with patch.object(eng_module, "GLib", sim.glib):
            self.assertFalse(engine._try_load_next_hour())


class CombinedIncidentRegressionTests(_RolloverFixture, TransactionTestCase):
    """Section 7: deleted Weather history + legacy PSA fill + rollover, end to end on the simulated clock."""

    def test_short_hour_is_filled_with_psas_then_rolls_over_once_without_overlap(self):
        from library.models import HourRefillInstall, HourRefillPolicy
        policies = list(HourRefillPolicy.objects.values())
        installs = HourRefillInstall.objects.count()
        rows = [(self.music_tracks[0], self.blues), (None, self.weather), (self.music_tracks[1], self.blues),
                (None, self.weather), (self.music_tracks[2], self.blues), (None, self.weather),
                (self.music_tracks[3], self.blues)]
        self.old_log.delete()                                        # replace the fixture's 21:00 log
        old_log = self.make_log(21, rows)
        sim = self.simulate(old_log=old_log, real_live_fill=True, end=at(22, 1, 30))
        report = "\n".join(f"{t:%H:%M:%S.%f} {slot} {title!r} by {who} other_live={o}"
                           for t, slot, _id, title, who, o in sim.starts)
        crashes = [e for e in sim.emitted if e.get("title") == "Live-fill worker crashed"]
        self.assertEqual(crashes, [], crashes and crashes[0]["detail"].get("traceback"))
        fill = list(LogItem.objects.filter(playlist_log=old_log, origin=LogItem.ORIGIN_LIVE_FILL).order_by("position"))
        self.assertTrue(fill, "no legacy PSA fill was installed\n" + report)
        self.assertEqual([i.position for i in fill], list(range(7, 7 + len(fill))))
        self.assertTrue(all(i.category_id == self.psa.id for i in fill))
        psa_starts = [s for s in sim.starts if s[2] in {i.id for i in fill}]
        self.assertTrue(psa_starts, report)
        self.assertGreaterEqual(psa_starts[0][0], at(21, 56, 39))          # after the final song, not over it
        next_titles = [s[3] for s in sim.starts if s[2] not in {i.id for i in fill}]
        self.assertEqual(next_titles[:3], ["Station ID", "1990 Intro", "Calendar of Events"], report)
        self.assertTrue(all(not s[5] for s in sim.starts), "overlapping program starts:\n" + report)
        self.assertLessEqual(sim.max_retry_chains, 1, report)
        self.assertEqual(sim.glib.retry_timers(sim.engine), [])
        # Continuous air from the final song onward. The PSA selection is random and legacy fill
        # lands within DURATION_FIT_MARGIN of the boundary by design, so the ONLY gap allowed is a
        # short one (<= that margin) ending at the next hour's first item; none anywhere else.
        first_next = next(item_id for _t, _s, item_id, title, *_ in sim.starts if title == "Station ID")
        intervals = sorted((t, t + timedelta(seconds=effective_airtime_seconds(
            LogItem.objects.select_related("track").get(id=item_id).track)), item_id)
            for t, _s, item_id, *_ in sim.starts)
        covered_until = at(21, 56, 39)
        for begin, finish, item_id in intervals:
            gap = (begin - covered_until).total_seconds()
            if item_id == first_next:
                self.assertLessEqual(gap, eng_module.DURATION_FIT_MARGIN, "dead air before %s\n%s" % (begin, report))
            else:
                self.assertLessEqual(gap, 1.0, "dead air before %s\n%s" % (begin, report))
            covered_until = max(covered_until, finish)
        # 2.27 stays observation-only: no policy change, no causal install, no refill rows.
        self.assertEqual(list(HourRefillPolicy.objects.values()), policies)
        self.assertEqual(HourRefillInstall.objects.count(), installs)
        self.assertFalse(LogItem.objects.filter(origin=LogItem.ORIGIN_REFILL_INSTALLED).exists())


# --------------------------------------------------------------------------- Codex corrective pass

class UnplayableSuccessorTests(_RolloverFixture, TransactionTestCase):
    """Correction A: a nonempty but entirely unplayable successor must never recurse."""

    def missing(self, title):
        artist, _ = Artist.get_or_create_ci(f"R135 missing {title}")
        return Track.objects.create(filepath=f"{self.media}/never-written-{title}.flac", filename=f"{title}.flac",
                                    title=title, artist=artist, category=self.weather, ready2air=True,
                                    duration_seconds=20.0, next_start_seconds=20.0)

    def cleared(self, title):
        track = self.missing(title)
        Track.objects.filter(id=track.id).update(filepath="")
        return Track.objects.get(id=track.id)

    def at_the_boundary(self, rows, **kwargs):
        # The previous hour is exhausted and idle; the successor is installed early.
        kwargs.setdefault("start", at(21, 58, 50))
        kwargs.setdefault("approve_at", at(21, 58, 51))
        kwargs.setdefault("end", at(21, 59, 30))
        return self.simulate(next_rows=rows, last_song_ends=None, **kwargs)

    def assert_bounded(self, sim, *, starts=()):
        report = "\n".join(f"{t:%H:%M:%S.%f} {slot} {title!r} by {who}" for t, slot, _i, title, who, _o in sim.starts)
        self.assertEqual(sim.runaway, [], f"recursive successor reloads: {sim.runaway}")
        self.assertLessEqual(sim.max_exhaust_depth, 1, "_on_log_exhausted re-entered itself")
        self.assertEqual([st[3] for st in sim.starts], list(starts), report)
        self.assertLessEqual(sim.max_retry_chains, 1)
        self.assertTrue(all(not st[5] for st in sim.starts), report)

    def test_ordinary_successor_of_only_deleted_tracks(self):
        sim = self.at_the_boundary([(None, self.weather)] * 4)
        self.assert_bounded(sim)
        self.assertEqual(len(sim.glib.retry_timers(sim.engine)), 1)              # still waiting, one owner
        self.assertIn("Approved log has no playable item", [e.get("title") for e in sim.emitted])

    def test_successor_whose_files_are_missing_from_disk(self):
        sim = self.at_the_boundary([(self.missing(f"m{i}"), self.weather) for i in range(3)])
        self.assert_bounded(sim)
        self.assertEqual(len(sim.glib.retry_timers(sim.engine)), 1)

    def test_mix_of_deleted_missing_and_cleared_tracks(self):
        rows = [(None, self.weather), (self.missing("a"), self.weather), (self.cleared("b"), self.weather), (None, self.psa)]
        self.assert_bounded(self.at_the_boundary(rows))

    def test_partial_hour_successor_with_no_playable_entries(self):
        ScheduleBlock.objects.filter(specific_date=WEDNESDAY, start_time=dt_time(22, 0)).delete()
        ScheduleBlock.objects.create(profile=ensure_schedule_profile_state().active_profile, specific_date=WEDNESDAY,
                                     start_time=dt_time(22, 30), end_time=dt_time(23, 0),
                                     rotation=Rotation.objects.first())
        sim = self.at_the_boundary([(None, self.weather), (self.missing("p"), self.weather)],
                                   start=at(22, 30, 1), approve_at=at(22, 30, 2), end=at(22, 31, 10), next_minute=30)
        self.assert_bounded(sim)
        self.assertEqual(len(sim.glib.retry_timers(sim.engine)), 1)

    def test_one_valid_item_after_several_unplayable_starts_once(self):
        rows = [(None, self.weather), (self.missing("x"), self.weather), self.next_rows[1], self.next_rows[2]]
        sim = self.at_the_boundary(rows, end=at(22, 0, 30))
        self.assert_bounded(sim, starts=("1990 Intro", "Calendar of Events"))

    def test_already_aired_items_are_never_replayed(self):
        def mark_first_aired(sim, engine):
            LogItem.objects.filter(playlist_log__hour=22, position=0).update(played_at=timezone.now())
        rows = [self.next_rows[0], (None, self.weather), self.next_rows[2]]
        sim = self.at_the_boundary(rows, approve_at=None, end=at(21, 59, 40),
                                   events=[(at(21, 58, 51), lambda sim, engine: (
                                       setattr(self, "next_log", self.make_log(22, rows, played=False)),
                                       mark_first_aired(sim, engine),
                                       sim.call("install", engine._install_built_hour, WEDNESDAY, 22)))])
        self.assert_bounded(sim, starts=("Calendar of Events",))

    def test_repeated_retries_while_the_successor_stays_unplayable(self):
        sim = self.simulate(next_rows=[(None, self.weather)] * 3, end=at(22, 3, 0))
        self.assert_bounded(sim)
        self.assertLessEqual(sim.timer_firings, 15)                           # one 30 s owner, nothing runaway
        self.assertEqual(len(sim.glib.retry_timers(sim.engine)), 1)

    def test_recovery_when_a_playable_item_becomes_available(self):
        late = self.missing("late-media")

        def restore_media(sim, engine):
            open(late.filepath, "wb").close()
        sim = self.simulate(next_rows=[(None, self.weather), (late, self.weather)], end=at(22, 1, 30),
                            events=[(at(22, 0, 20), restore_media)])
        self.assert_bounded(sim, starts=("late-media",))
        self.assertGreaterEqual(sim.starts[0][0], at(22, 0, 20))

    def test_recovery_when_a_later_valid_successor_is_installed(self):
        ScheduleBlock.objects.create(profile=ensure_schedule_profile_state().active_profile, specific_date=WEDNESDAY,
                                     start_time=dt_time(23, 0), end_time=dt_time(0, 0), rotation=Rotation.objects.first())
        dead = self.make_log(22, [(None, self.weather), (self.missing("d"), self.weather)], played=False)
        sim = self.simulate(old_log=dead, start=at(22, 58, 50), last_song_ends=None, approve_at=at(22, 59, 34),
                            next_hour=23, end=at(23, 0, 10))
        self.assert_bounded(sim, starts=("Station ID", "1990 Intro"))
        self.assertEqual([st[4] for st in sim.starts], ["install", "eos"])


class ManualHoldRetryTests(_RolloverFixture, TransactionTestCase):
    """Correction B: an exhausted-log retry never starts scheduled playback during Manual."""

    @staticmethod
    def manual(on):
        def toggle(sim, engine):
            engine.manual_mode = on
        return toggle

    def assert_held(self, sim, *, flags=None):
        report = "\n".join(f"{t:%H:%M:%S.%f} {slot} {title!r} by {who}" for t, slot, _i, title, who, _o in sim.starts)
        self.assertEqual(sim.starts, [], "a retry started playback during Manual:\n" + report)
        self.assertEqual(len(sim.glib.retry_timers(sim.engine)), 1)            # recovery kept, one owner
        self.assertLessEqual(sim.max_retry_chains, 1)
        self.assertFalse(LogItem.objects.filter(playlist_log__hour=22, played_at__isnull=False).exists())
        if flags is not None:
            engine = sim.engine
            self.assertEqual((engine.manual_mode, engine._manual_from_mic, engine._manual_hold_pending), flags)

    def test_retry_armed_in_auto_then_manual_before_it_fires(self):
        def ownership(sim, engine):
            engine._manual_from_mic = True                                 # a mic-owned hold
        sim = self.simulate(approve_at=at(21, 57, 30), end=at(21, 59, 0),
                            events=[(at(21, 57, 0), self.manual(True)), (at(21, 57, 0), ownership)])
        self.assertGreaterEqual(sim.timer_firings, 3)                       # several intervals in Manual
        self.assert_held(sim, flags=(True, True, False))
        engine = sim.engine
        # Nothing handed out: either the exhausted 21:00 queue, or the approved
        # 22:00 queue installed (not started) by the build path, at its first row.
        if engine.current_log.hour == 22:
            self.assertEqual(engine._queue_cursor, 0)
        else:
            self.assertEqual(engine._queue_cursor, len(engine.log_items))

    def test_retry_armed_while_a_manual_hold_already_exists(self):
        def hold(sim, engine):
            engine.manual_mode = True
            engine._manual_hold_pending = True
        sim = self.simulate(approve_at=at(21, 57, 30), end=at(21, 59, 0), last_song_ends=None,
                            setup=hold,
                            events=[(at(21, 56, 40), lambda sim, engine: sim.call("exhausted", engine._on_log_exhausted, "A"))])
        self.assert_held(sim, flags=(True, False, True))

    def test_return_to_auto_with_a_playable_queue_resumes_once(self):
        sim = self.simulate(approve_at=at(21, 57, 30), end=at(21, 59, 10),
                            events=[(at(21, 57, 0), self.manual(True)), (at(21, 58, 20), self.manual(False))])
        titles = [st[3] for st in sim.starts]
        self.assertEqual(titles, ["Station ID", "1990 Intro"])
        self.assertGreaterEqual(sim.starts[0][0], at(21, 58, 20))
        self.assertTrue(all(not st[5] for st in sim.starts))
        self.assertLessEqual(sim.max_retry_chains, 1)

    def test_return_to_auto_while_the_successor_is_unavailable(self):
        sim = self.simulate(approve_at=None, end=at(21, 59, 0),
                            events=[(at(21, 57, 0), self.manual(True)), (at(21, 58, 0), self.manual(False))])
        self.assertEqual(sim.starts, [])
        self.assertEqual(len(sim.glib.retry_timers(sim.engine)), 1)

    def test_manual_toggled_around_a_retry_firing(self):
        # Armed at 21:56:39.25 by the final song's end -> fires 21:57:09.25 inside a hold
        # that began before approval (so the install path itself cannot start) and ends
        # 0.25 s after that firing.
        sim = self.simulate(approve_at=at(21, 56, 50), end=at(21, 58, 10),
                            events=[(at(21, 56, 45), self.manual(True)), (at(21, 57, 9, 500000), self.manual(False))])
        self.assertTrue(sim.starts)
        self.assertGreater(sim.starts[0][0], at(21, 57, 9, 500000))         # nothing started inside the hold
        titles = [st[3] for st in sim.starts]
        self.assertEqual(titles[:2], ["Station ID", "1990 Intro"])
        self.assertEqual(len(titles), len(set(titles)))                      # no duplicate start
        self.assertTrue(all(not st[5] for st in sim.starts))

    def test_shutdown_with_a_pending_retry_during_manual(self):
        def stop(sim, engine):
            engine.running = False
            engine._cancel_exhausted_retry()
        sim = self.simulate(approve_at=at(21, 57, 30), end=at(21, 59, 0),
                            events=[(at(21, 57, 0), self.manual(True)), (at(21, 57, 20), stop)])
        self.assertEqual(sim.starts, [])
        self.assertEqual(sim.glib.retry_timers(sim.engine), [])

    def test_manual_release_start_and_retry_never_double_start(self):
        def hold(sim, engine):
            engine.manual_mode = True
            engine._manual_hold_pending = True
        sim = self.simulate(approve_at=at(21, 57, 30), end=at(21, 58, 40), setup=hold,
                            events=[(at(21, 58, 5), lambda sim, engine: sim.call("release", engine._set_manual_mode, False))])
        self.assertEqual([st[3] for st in sim.starts], ["Station ID", "1990 Intro"])
        self.assertEqual(sim.starts[0][4], "release")
        self.assertTrue(all(not st[5] for st in sim.starts))
        self.assertLessEqual(sim.max_retry_chains, 1)


# --------------------------------------------------------------------------- Codex second review

_QUEUE_RELATED = ("track", "track__artist", "track__album", "track__category", "track__category__kind",
                  "category", "category__kind")


class UrgentOccurrenceRestartTests(TwoHourFixture):
    """Correction A (second review): a pending urgent insertion survives a restart; aired rows never replay.

    Old hour 00:00: N (17) on air, N+1 (18) armed as a forced follow-up (cursor on it, exactly as a dedication
    leaves it), N+2 (19). Urgent A, then urgent B, go through the real _insert_urgent_next_inner: both are
    spliced at the cursor and B is prepended ahead of A in _forced_next_items, so B (persisted AFTER A) airs
    first. The process then dies before A airs."""

    def setUp(self):
        super().setUp()
        self.urgent_a = self.make_urgent("R135URGA", "Urgent A (older)")
        self.urgent_b = self.make_urgent("R135URGB", "Urgent B (newer)")

    def make_urgent(self, code, title):
        category = Category.objects.create(code=code, name=title, kind=self.category.kind)
        track = self.make_track(title)
        Track.objects.filter(id=track.id).update(category=category)
        return category

    @staticmethod
    def queue(log):
        return list(log.items.select_related(*_QUEUE_RELATED).order_by("position"))

    def first_process(self):
        """Run the previous process up to B on air; return (engine, A row, B row)."""
        engine = make_minimal_stand_in()
        engine._engine_session_id = "previous-engine-session"
        engine._urgent_retry_counts = {}
        engine._resume_item = None
        engine._resume_hint = None
        engine._poison_skip_identities = []
        engine.current_log = self.log
        engine.log_items = self.queue(self.log)
        engine._queue_cursor = 1
        engine._forced_next_items = [engine.log_items[1]]           # N+1 armed as a forced follow-up
        with patch.object(eng_module.timezone, "localtime", return_value=timezone.now()):
            engine._insert_urgent_next_inner(self.urgent_a.code)
            engine._insert_urgent_next_inner(self.urgent_b.code)
        a_row, b_row = engine.log_items[1], engine.log_items[2]
        self.assertEqual((a_row.track.category_id, b_row.track.category_id), (self.urgent_a.id, self.urgent_b.id))
        self.assertEqual([i.id for i in engine._forced_next_items], [b_row.id, a_row.id, self.item_next.id])
        # N ends: the real selection hands out B first.
        engine._create_deck = MagicMock()
        item, forced = engine._next_queue_item()
        self.assertEqual((item.id, forced), (b_row.id, True))
        b_row.played_at = timezone.now() - timedelta(seconds=3)    # first real buffer committed
        LogItem.objects.filter(id=b_row.id).update(played_at=b_row.played_at)
        deck = Deck("A", b_row.track, b_row, MagicMock(), MagicMock(), generation=4)
        deck.media_buffer_count = 120
        engine.decks = {"A": deck, "B": None}
        engine._get_deck_position = lambda deck: 3.0
        return engine, a_row, b_row

    def restart(self, *, resume):
        engine, a_row, b_row = self.first_process()
        if resume:
            engine._write_state(transport="PLAYING")              # the real snapshot, B actually on air
        fresh = self.engine()
        self.recover(fresh)
        return fresh, a_row, b_row

    def assert_order(self, created, expected):
        ids = [i for i, _ in created]
        self.assertEqual(ids, [i.id for i in expected])
        self.assertEqual(len(ids), len(set(ids)), "an occurrence was handed out twice")

    def test_restart_while_newer_urgent_is_on_air_resumes_it_then_airs_the_older_urgent_once(self):
        fresh, a_row, b_row = self.restart(resume=True)
        self.assertEqual(fresh._resume_item.id, b_row.id)
        created = self.start_tracks(fresh, 4)
        self.assertEqual(created[0], (b_row.id, True))             # exact resume hint still honoured
        self.assert_order(created, [b_row, a_row, self.item_next, self.item_n2])

    def test_restart_after_newer_urgent_aired_airs_the_older_urgent_once(self):
        fresh, a_row, b_row = self.restart(resume=False)
        self.assertIsNone(fresh._resume_hint)
        created = self.start_tracks(fresh, 3)
        self.assert_order(created, [a_row, self.item_next, self.item_n2])
        self.assertNotIn(b_row.id, [i for i, _ in created])

    def test_periodic_queue_reload_between_restart_and_selection_keeps_the_pending_urgent(self):
        fresh, a_row, b_row = self.restart(resume=False)
        fresh._last_queue_reload = 0
        fresh._reload_queue_if_changed()
        self.assertEqual(fresh.log_items[fresh._queue_cursor].id, a_row.id)
        self.assertEqual([i.id for i in fresh._get_upcoming_preview()][:2], [a_row.id, self.item_next.id])
        self.assertTrue(fresh._has_playable_at_cursor())
        self.assert_order(self.start_tracks(fresh, 3), [a_row, self.item_next, self.item_n2])

    def test_queue_reload_while_the_resumed_occurrence_occupies_a_deck_keeps_the_pending_urgent(self):
        fresh, a_row, b_row = self.restart(resume=True)
        item, forced = fresh._next_queue_item()                    # the resume item goes to a deck ...
        self.assertEqual((item.id, forced), (b_row.id, True))
        fresh.decks = {"A": SimpleNamespace(log_item=item, track=item.track), "B": None}
        fresh._last_queue_reload = 0
        fresh._reload_queue_if_changed()                           # ... and the ~3 s reload runs under it
        self.assertEqual(fresh.log_items[fresh._queue_cursor].id, a_row.id)
        self.assertEqual(fresh._peek_playable_at_cursor().id, a_row.id)
        item, forced = fresh._next_queue_item()
        self.assertEqual((item.id, forced), (a_row.id, False))
        fresh.decks = {"A": None, "B": SimpleNamespace(log_item=item, track=item.track)}
        fresh._last_queue_reload = 0
        fresh._reload_queue_if_changed()
        self.assertEqual(fresh._peek_playable_at_cursor().id, self.item_next.id)   # B passed over, not replayed
        self.assertEqual(fresh._next_queue_item()[0].id, self.item_next.id)

    def test_queue_reload_with_a_deck_occupied_never_jumps_a_pending_row(self):
        """The reload in isolation: cursor on pending A, aired B on a deck after it."""
        _engine, a_row, b_row = self.first_process()
        fresh = self.engine()
        fresh._poison_skip_identities = []
        fresh.current_log = self.log
        fresh.log_items = self.queue(self.log)
        fresh._queue_cursor = [i.id for i in fresh.log_items].index(a_row.id)
        on_deck = next(i for i in fresh.log_items if i.id == b_row.id)
        fresh.decks = {"A": SimpleNamespace(log_item=on_deck, track=on_deck.track), "B": None}
        fresh._last_queue_reload = 0
        fresh._reload_queue_if_changed()
        self.assertEqual(fresh.log_items[fresh._queue_cursor].id, a_row.id)

    def test_queue_reload_still_moves_past_rows_that_aired_or_are_on_a_deck(self):
        """Ordinary forward correction is unchanged: the cursor never rests on an aired/on-deck row."""
        engine = self.engine()
        engine._poison_skip_identities = []
        engine.current_log = self.log
        engine.log_items = self.queue(self.log)
        engine._queue_cursor = 0                                   # stale: N (aired) is on a deck
        engine.decks = {"A": SimpleNamespace(log_item=engine.log_items[0], track=self.track_n), "B": None}
        engine._last_queue_reload = 0
        engine._reload_queue_if_changed()
        self.assertEqual(engine.log_items[engine._queue_cursor].id, self.item_next.id)

    def test_pending_native_insertion_behind_a_newer_emergency_survives_restart(self):
        """Engine side only (domain admission/recording stubbed; the active_messages suites cannot run here:
        dev media validation rejects their fixtures, identically on 622033f). The real splice + forced-order
        path: native M is queued behind nothing, a later WxAlert W keeps emergency priority ahead of it, W airs,
        the process dies before M airs."""
        wx, _ = Category.objects.get_or_create(code="WxAlert", defaults={"name": "WxAlert", "kind": self.category.kind})
        Track.objects.filter(id=self.make_track("Weather Alert").id).update(category=wx)
        native_track = self.make_track("Native Active Message")
        engine = make_minimal_stand_in()
        engine._urgent_retry_counts = {}
        engine._resume_item = None
        engine._poison_skip_identities = []
        engine.current_log = self.log
        engine.log_items = self.queue(self.log)
        engine._queue_cursor = 1
        engine._forced_next_items = [engine.log_items[1]]
        admission = SimpleNamespace(track=native_track, category=self.category, attempt="r0135-attempt")
        with patch("active_messages.playout.admit", return_value=admission), \
                patch("active_messages.playout.record_insertion"), \
                patch.object(eng_module.timezone, "localtime", return_value=timezone.now()):
            engine._insert_active_message_inner({"attempt": "r0135-attempt", "active_execution": 1})
            engine._insert_urgent_next_inner("WxAlert")
        native_row, wx_row = engine.log_items[1], engine.log_items[2]
        self.assertEqual([i.id for i in engine._forced_next_items], [wx_row.id, native_row.id, self.item_next.id])
        self.assertEqual(engine._next_queue_item()[0].id, wx_row.id)
        LogItem.objects.filter(id=wx_row.id).update(played_at=timezone.now())
        fresh = self.engine()
        self.recover(fresh)
        self.assert_order(self.start_tracks(fresh, 3), [native_row, self.item_next, self.item_n2])

    def add_history(self, rows):
        """rows: (position, track, aired) placed before N (17)."""
        made = []
        for position, track, aired in rows:
            made.append(self.make_item(position, track, played_at=timezone.now() - timedelta(minutes=30 - position)
                                       if aired else None,
                                       scheduled_time=timezone.now() - timedelta(minutes=30 - position)))
        return made

    def gone(self, title):
        track = self.make_track(title)
        Path(track.filepath).unlink()
        return track

    def test_unplayable_earlier_row_before_an_aired_valid_row_never_replays_it(self):
        unplayable, aired = self.add_history([(15, self.gone("Gone Weather"), False),
                                              (16, self.make_track("Aired Valid"), True)])
        engine = self.engine()
        self.recover(engine)
        created = self.start_tracks(engine, 2)
        self.assert_order(created, [self.item_next, self.item_n2])

    def test_several_skipped_historical_rows_cause_no_repeated_playback(self):
        history = self.add_history([(10, self.gone("Gone 1"), False), (11, self.make_track("Aired 1"), True),
                                    (12, self.gone("Gone 2"), False), (13, self.make_track("Aired 2"), True),
                                    (14, self.make_track("Skipped Ordinary"), False),
                                    (15, self.make_track("Aired 3"), True)])
        handed = []
        with patch.object(eng_module, "emit_event") as emitted:
            for _restart in range(3):
                engine = self.engine()
                self.recover(engine)
                engine._last_queue_reload = 0
                engine._reload_queue_if_changed()
                self.assertTrue(all(i.played_at is None for i in engine._get_upcoming_preview()))
                handed += [i for i, _ in self.start_tracks(engine, 1)]
        self.assertEqual(handed, [self.item_next.id] * 3)          # never a historical row
        self.assertFalse(set(handed) & {row.id for row in history})
        self.assertFalse([c for c in emitted.call_args_list if "Skipped log item" in c.kwargs.get("title", "")])

    def test_ordinary_consecutive_playback_stays_in_order_through_reloads(self):
        LogItem.objects.filter(id=self.item_n.id).update(played_at=None)
        engine = self.engine()
        self.recover(engine)
        handed = []
        for _ in range(3):
            engine._last_queue_reload = 0
            engine._reload_queue_if_changed()
            handed += [i for i, _ in self.start_tracks(engine, 1)]
        self.assertEqual(handed, [self.item_n.id, self.item_next.id, self.item_n2.id])

    def test_valid_unaired_rows_after_aired_rows_remain_eligible(self):
        engine = self.engine()
        self.recover(engine)
        self.assertEqual([i.id for i in engine._get_upcoming_preview()][:2], [self.item_next.id, self.item_n2.id])
        self.assert_order(self.start_tracks(engine, 2), [self.item_next, self.item_n2])


class NonReportingPoisonProbeTests(_RolloverFixture, TransactionTestCase):
    """Correction B (second review): the eligibility probe is membership only."""

    def poisoned_engine(self):
        log = self.make_log(22, self.next_rows[:2], played=False)
        engine = object.__new__(eng_module.PlaybackEngine)
        engine.decks = {"A": None, "B": None}
        engine.current_log = log
        engine.log_items = self.reload_items(log)
        engine._queue_cursor = 0
        engine._forced_next_items = []
        engine._resume_item = None
        engine._poison_skip_identities = [(log.id, engine.log_items[0].id)]
        engine._offer_automatic_skip_evidence = MagicMock()
        engine._note_active_message_discard = MagicMock()
        engine._try_extend_live_log_async = MagicMock(return_value=False)
        return engine

    def test_probe_refuses_a_poisoned_item_without_any_side_effect(self):
        engine = self.poisoned_engine()
        engine.log_items = engine.log_items[:1]                    # only the poisoned occurrence
        engine._forced_next_items = list(engine.log_items)          # and a forced copy of it
        before = (engine._queue_cursor, [i.id for i in engine.log_items], [i.id for i in engine._forced_next_items])
        with patch.object(eng_module, "emit_event") as emitted, \
                CaptureQueriesContext(connection) as queries:
            self.assertFalse(engine._has_playable_at_cursor())
        emitted.assert_not_called()
        self.assertEqual(len(queries), 0, [q["sql"] for q in queries])
        self.assertEqual(before, (engine._queue_cursor, [i.id for i in engine.log_items],
                                  [i.id for i in engine._forced_next_items]))
        self.assertFalse(getattr(engine, "_poison_reported_identities", set()))
        engine._offer_automatic_skip_evidence.assert_not_called()

    def test_normal_selection_still_refuses_and_reports_the_poisoned_item_once(self):
        engine = self.poisoned_engine()
        poisoned, valid = engine.log_items
        with patch.object(eng_module, "emit_event") as probed:
            for _ in range(3):
                self.assertTrue(engine._has_playable_at_cursor())   # the valid row behind it
        probed.assert_not_called()                                   # the probe never spends the once-only report
        with patch.object(eng_module, "emit_event") as emitted:
            self.assertEqual(engine._peek_playable_at_cursor().id, valid.id)
            engine._queue_cursor = 0
            self.assertEqual(engine._next_queue_item()[0].id, valid.id)
            engine._queue_cursor = 0
            engine._peek_playable_at_cursor()
        poison_events = [c.kwargs for c in emitted.call_args_list
                         if c.kwargs.get("title") == "Refused poison-guarded LogItem after policy restart"]
        self.assertEqual(len(poison_events), 1)                      # once-only, first REAL encounter
        self.assertEqual(poison_events[0]["detail"]["log_item_id"], poisoned.id)

    def test_only_poisoned_and_unplayable_successor_stays_bounded(self):
        rows = [self.next_rows[0], (None, self.weather)]

        def install_poisoned(sim, engine):
            self.next_log = self.make_log(22, rows, played=False)
            first = self.next_log.items.order_by("position").first()
            engine._poison_skip_identities = [(self.next_log.id, first.id)]
            engine._apply_poison_skip = lambda items: items      # keep it materialized: the selection gate must act
            sim.call("install", engine._install_built_hour, WEDNESDAY, 22)

        sim = self.simulate(approve_at=None, start=at(21, 58, 50), last_song_ends=None, end=at(22, 2, 0),
                            events=[(at(21, 58, 51), install_poisoned)])
        self.assertEqual(sim.runaway, [])
        self.assertLessEqual(sim.max_exhaust_depth, 1)
        self.assertEqual(sim.starts, [])
        self.assertLessEqual(sim.max_retry_chains, 1)
        self.assertLessEqual(sim.timer_firings, 10)
        self.assertEqual(len([e for e in sim.emitted
                              if e.get("title") == "Refused poison-guarded LogItem after policy restart"]), 1)
