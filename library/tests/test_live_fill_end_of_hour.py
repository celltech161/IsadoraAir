"""Legacy live fill at the end of the hour: the closing fill (post-r0135).

Before this change the real-time legacy fallback ignored any end-of-hour deficit of 30 s or less
(``DURATION_FIT_MARGIN``) twice over: the dispatcher suppressed it and ``fill_remaining_hour``
returned immediately. Whether that produced silence depends on the next-hour lookahead: the next
hour's log is built and approved about 23-26 s before the top of the hour (KOGR journal, 2026-11-12:
:59:34-:59:37). Once approved it replaces the unplayed queue and starts as soon as the current item
ends, so a remainder shorter than that lead was already covered by an early rollover. Silence was
possible only when the queue ran out before the approval -- a remainder between that lead and 30 s,
or any remainder when the next hour was late or not approved at all.

The closing fill lets the live dispatcher fill a remainder longer than the lookahead window can cover
(> NEXT_HOUR_LOOKAHEAD_SECONDS - AUTO_BUILD_CHECK_SECONDS = 20 s: the next hour's build starts on the
first scheduler tick inside the last 30 s, so no later than 20 s before the hour) with a fallback item
that ends at or before the top of the hour (zero overshoot, strict separation), re-validated against
the same deadline when it is installed. Remainders of 20 s or less stay with the early rollover. The scenarios run the real dispatcher, worker, fill_remaining_hour,
installer, rollover, retry and scheduler-tick code on the r0135 simulated clock, with stand-in decks;
the silence they record is measured from simulated deck start/end times, not taken from log rows.
"""
import random
import threading
import time as _real_time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import library.services.engine as eng_module
import library.services.log_builder as log_builder
from library.models import Artist, LogItem, PlaylistLog, Track
from library.services.hour_refill_install import RefillCoordinator
from library.services.log_builder import DURATION_FIT_MARGIN, effective_airtime_seconds, fill_remaining_hour, pick_track
from library.tests.test_r0135_playout_continuity import (   # fixtures and helpers only: no TestCase is imported
    WEDNESDAY, _RolloverFixture, _SyncThread, _live_fill_engine, at,
)


TOH = at(22, 0)
# A KOGR-shaped PSA pool (2026-11-12, read-only: 78 Ready-to-Air PSAs, 15 at 13.4-14.2 s, 50 at 25-30 s,
# 12 at 45-59 s), reduced to a few representatives.
KOGR_POOL = (13.4, 14.1, 25.8, 28.5, 29.2, 30.0, 33.5, 58.9, 59.3)


SIM_STEP_SECONDS = 0.25      # RolloverSim's step


def _closest(items, _weight_fn):
    """Deterministic stand-in for the weighted choice among best-fit finalists: the closest fit.
    Removes randomness only; the eligible pool, separation and overshoot rules are untouched."""
    return items[0]


class _SimClock:
    """``time`` module proxy whose time() follows the simulation clock (the dispatcher's 5 s throttle)."""

    def __init__(self, holder):
        self._holder = holder

    def time(self):
        sim = self._holder.get("sim")
        return sim.now.timestamp() if sim is not None else _real_time.time()

    def __getattr__(self, name):
        return getattr(_real_time, name)


class _EndOfHourFixture(_RolloverFixture):

    def use_pool(self, *durations):
        """Only these fallback (PSA) items are Ready to Air; each has its own sponsor/artist."""
        Track.objects.filter(category=self.psa).update(ready2air=False)
        made = []
        for i, seconds in enumerate(durations):
            artist, _ = Artist.get_or_create_ci(f"EOH Sponsor {seconds} {i}")
            path = f"{self.media}/eoh_psa_{i}_{seconds}.flac"
            open(path, "wb").close()
            made.append(Track.objects.create(
                filepath=path, filename=f"eoh_psa_{i}.flac", title=f"PSA {seconds}s #{i}", artist=artist,
                category=self.psa, ready2air=True, duration_seconds=seconds, next_start_seconds=seconds,
            ))
        return made

    def simulate_eoh(self, deficit, *, lead=23.0, lead_in=30.0, until=None, approve_at=None, old_log=None, setup=None, events=()):
        """The final scheduled song ends `deficit` seconds before the top of the hour; the next hour's log is
        approved `lead` seconds before it (None: never). Returns (sim, metrics)."""
        final_end = TOH - timedelta(seconds=deficit)
        if approve_at is None and lead is not None:
            approve_at = TOH - timedelta(seconds=lead)
        holder = {"workers": 0, "delivered": 0}

        def hook(sim, engine):
            holder["sim"] = sim
            real_worker, real_install = engine._live_fill_worker, engine._install_live_fill

            def counted_worker(*args, **kwargs):
                holder["workers"] += 1
                return real_worker(*args, **kwargs)

            def counted_install(*args):
                holder["delivered"] += 1
                return real_install(*args)
            engine._live_fill_worker, engine._install_live_fill = counted_worker, counted_install
            if setup is not None:
                setup(sim, engine)

        end = until or (TOH + timedelta(seconds=15) if lead is not None else TOH - timedelta(seconds=1))
        with patch.object(eng_module, "time", _SimClock(holder)), \
                patch.object(log_builder, "_weighted_choice_by", _closest):
            sim = self.simulate(
                old_log=old_log, real_live_fill=True, last_song_ends=final_end, approve_at=approve_at,
                # First poll lands on a whole second, so the dispatcher's whole-second clock reads exactly.
                start=final_end - timedelta(seconds=lead_in, milliseconds=250),
                end=end, setup=hook, events=events,
            )
        sim.workers, sim.delivered = holder["workers"], holder["delivered"]
        return sim, self.measure(sim, final_end)

    def measure(self, sim, final_end):
        rows = {li.id: li for li in LogItem.objects.select_related("track", "playlist_log")
                .filter(id__in=[s[2] for s in sim.starts])}
        timeline = sorted(
            (t, t + timedelta(seconds=effective_airtime_seconds(rows[item_id].track)), rows[item_id], other)
            for t, _slot, item_id, _title, _who, other in sim.starts)
        covered, silence, first_next = final_end, 0.0, None
        for begin, finish, item, _other in timeline:
            gap = (begin - covered).total_seconds()
            # A gap shorter than one 250 ms simulation step is the step quantisation of a fractional
            # airtime (an EOS is noticed on the next step), not silence; anything longer counts in full.
            silence += gap if gap >= SIM_STEP_SECONDS else 0.0
            covered = max(covered, finish)
            if item.playlist_log.hour == 22:
                first_next = begin
                break
        if first_next is None:
            silence += max(0.0, (sim.now - covered).total_seconds())
        old_hour = [(b, f, i) for b, f, i, _o in timeline if i.playlist_log.hour == 21]
        aired_fill = [(b, f, i) for b, f, i in old_hour if i.origin == LogItem.ORIGIN_LIVE_FILL]
        fill_rows = list(LogItem.objects.filter(playlist_log__hour=21, origin=LogItem.ORIGIN_LIVE_FILL).order_by("position"))
        return SimpleNamespace(
            silence=round(silence, 2),
            next_offset=None if first_next is None else round((first_next - TOH).total_seconds(), 2),
            final_offset=round((final_end - TOH).total_seconds(), 2),
            fill_overrun=round(max([(f - TOH).total_seconds() for _b, f, _i in aired_fill] + [-3600.0]), 2),
            aired_fill=[effective_airtime_seconds(i.track) for _b, _f, i in aired_fill],
            fill_rows=fill_rows,
            overlaps=[i.id for _b, _f, i, other in timeline if other],
            report="\n".join(f"{b:%H:%M:%S.%f} -> {f:%H:%M:%S.%f} h{i.playlist_log.hour} {i.origin} {i.track_title!r}"
                             for b, f, i, _o in timeline),
        )

    def assert_clean_boundary(self, sim, m):
        self.assertEqual(m.overlaps, [], "overlapping starts\n" + m.report)
        self.assertLessEqual(sim.max_retry_chains, 1, m.report)
        crashes = [e for e in sim.emitted if e.get("title") == "Live-fill worker crashed"]
        self.assertEqual(crashes, [], crashes and crashes[0]["detail"].get("traceback"))
        # Live fill never runs past the top of the hour, and approved next-hour programming starts no later
        # than the scheduled material before it allows (1 s allowance for install-time jitter).
        self.assertLessEqual(m.fill_overrun, 1.0, m.report)
        if m.next_offset is not None:
            self.assertLessEqual(m.next_offset, max(0.0, m.final_offset) + 1.0, m.report)
        # No LogItem is duplicated: positions in the old hour stay unique and contiguous.
        positions = list(LogItem.objects.filter(playlist_log__hour=21).order_by("position").values_list("position", flat=True))
        self.assertEqual(positions, list(range(len(positions))))


class EndOfHourClosingFillTests(_EndOfHourFixture, TransactionTestCase):
    """Workorder section 6, scenarios 1-9 and 14, plus the section-5 negative controls."""

    def test_s01_120s_shortage_is_filled_by_the_existing_mechanism(self):
        self.use_pool(58.0, 59.0)
        sim, m = self.simulate_eoh(120.0)
        self.assert_clean_boundary(sim, m)
        self.assertEqual(sorted(m.aired_fill), [58.0, 59.0], m.report)   # the unchanged ordinary proposal
        self.assertEqual(m.silence, 0.0, m.report)

    def test_s02_25s_deficit_is_closed_by_an_eligible_15s_psa(self):
        self.use_pool(15.0, 29.0, 45.0, 59.0)
        sim, m = self.simulate_eoh(25.0)
        self.assert_clean_boundary(sim, m)
        self.assertEqual(m.aired_fill, [15.0], m.report)
        self.assertEqual(m.silence, 0.0, m.report)          # r0135: 2 s (song ends :59:35, approval :59:37)
        self.assertEqual(m.next_offset, -10.0, m.report)   # next hour crossfades in when the PSA ends

    def test_s03_30s_deficit_is_closed_by_a_matching_30s_psa_landing_on_the_hour(self):
        self.use_pool(14.0, 29.0, 30.0, 45.0)
        sim, m = self.simulate_eoh(30.0)
        self.assert_clean_boundary(sim, m)
        self.assertEqual(m.aired_fill, [30.0], m.report)
        self.assertEqual(m.silence, 0.0, m.report)          # r0135: 7 s
        self.assertEqual(m.next_offset, 0.0, m.report)

    def test_s04_17s_deficit_is_left_to_the_next_hour_lookahead(self):
        self.use_pool(15.0, 29.0, 59.0)
        # Ordinary approval timing (23 s out, before the song ends 17 s out): the early rollover covers the
        # remainder, exactly as on r0135. No closing fill is dispatched, so the old hour gains no row the
        # rollover would supersede unaired.
        sim, m = self.simulate_eoh(17.0)
        self.assert_clean_boundary(sim, m)
        self.assertEqual((m.silence, m.next_offset, sim.workers, m.fill_rows), (0.0, -17.0, 0, []), m.report)
        LogItem.objects.filter(playlist_log__hour=22).delete()
        PlaylistLog.objects.filter(hour=22).delete()
        # Documented limitation: a build that approves only 8 s out leaves 9 s of silence, as on r0135.
        sim, m = self.simulate_eoh(17.0, lead=8.0)
        self.assert_clean_boundary(sim, m)
        self.assertEqual((m.silence, sim.workers, m.fill_rows), (9.0, 0, []), m.report)

    def test_s05_remainder_shorter_than_every_eligible_psa_is_attempted_once(self):
        self.use_pool(29.0, 30.0, 59.0)
        sim, m = self.simulate_eoh(25.0)
        self.assert_clean_boundary(sim, m)
        self.assertEqual((m.aired_fill, m.fill_rows), ([], []), m.report)
        self.assertEqual(m.silence, 2.0, m.report)          # documented limitation: nothing short enough
        self.assertEqual(sim.workers, 1, "a no-fit closing deficit must not be re-dispatched")
        # Below the closing floor nothing is attempted at all.
        LogItem.objects.filter(playlist_log__hour=22).delete()
        PlaylistLog.objects.filter(hour=22).delete()
        sim, m = self.simulate_eoh(12.0, lead=5.0)
        self.assertEqual((sim.workers, m.fill_rows, m.silence), (0, [], 7.0), m.report)

    def test_s06_zero_and_negative_deficits_never_dispatch(self):
        self.use_pool(15.0, 29.0)
        for deficit in (0.0, -10.0, 4.0, 20.0):
            with self.subTest(deficit=deficit):
                LogItem.objects.filter(playlist_log__hour=22).delete()
                PlaylistLog.objects.filter(hour=22).delete()
                sim, m = self.simulate_eoh(deficit)
                self.assert_clean_boundary(sim, m)
                self.assertEqual((sim.workers, m.fill_rows), (0, []), m.report)

    def test_s07_long_final_track_with_the_cursor_at_the_queue_end_is_not_a_false_deficit(self):
        self.use_pool(15.0, 29.0)
        # The cursor reached the queue end ~25 minutes before the hour; the final item carries the hour.
        sim, m = self.simulate_eoh(-8.0, lead_in=1500.0, until=TOH + timedelta(seconds=20))
        self.assertEqual((sim.workers, m.fill_rows), (0, []), m.report)
        self.assert_clean_boundary(sim, m)

    def test_s08_next_hour_already_approved_suppresses_any_extension(self):
        self.use_pool(15.0, 29.0)
        # Approved before the final item was dequeued (the first poll): nothing is dispatched at all.
        sim, m = self.simulate_eoh(25.0, approve_at=TOH - timedelta(seconds=175), lead_in=150.0)
        self.assert_clean_boundary(sim, m)
        self.assertEqual((sim.workers, m.fill_rows, m.silence, m.next_offset), (0, [], 0.0, -25.0), m.report)

    def test_s09_next_hour_not_approved_closing_psa_still_airs(self):
        self.use_pool(15.0, 29.0)
        sim, m = self.simulate_eoh(25.0, lead=None)
        self.assert_clean_boundary(sim, m)
        self.assertEqual(m.aired_fill, [15.0], m.report)
        self.assertEqual(m.silence, 9.0, m.report)          # the last 10 s before :00 (to :59:59) stay unfilled
        self.assertLessEqual(m.fill_overrun, 0.0)

    def test_s14_missing_historical_track_rows_in_the_queue(self):
        self.use_pool(15.0, 29.0)
        rows = [(self.music_tracks[0], self.blues), (None, self.weather), (self.music_tracks[1], self.blues),
                (None, self.weather), (self.music_tracks[2], self.blues)]
        self.old_log.delete()
        sim, m = self.simulate_eoh(25.0, old_log=self.make_log(21, rows))
        self.assert_clean_boundary(sim, m)
        self.assertEqual((m.aired_fill, m.silence), ([15.0], 0.0), m.report)
        self.assertEqual(LogItem.objects.filter(playlist_log__hour=21, track__isnull=True).count(), 2)

    def test_nc2_larger_shortage_reduced_to_a_small_remainder_is_closed(self):
        self.use_pool(15.0, 70.0)
        # 98 s: the ordinary fill takes the 70 s item (closest) and stops 28 s short. r0135 never revisits
        # that remainder (5 s silence with a 23 s approval lead); the closing fill adds the 15 s item.
        sim, m = self.simulate_eoh(98.0)
        self.assert_clean_boundary(sim, m)
        self.assertEqual(m.aired_fill, [70.0, 15.0], m.report)
        self.assertEqual(m.silence, 0.0, m.report)
        self.assertLessEqual(sim.workers, 3)

    def test_s16_final_item_dequeued_inside_the_last_30s(self):
        """A short final item started 29 s before the hour leaves a 26 s shortage the dispatcher first sees
        inside the final 30 s (ba3f8a4: suppressed by the wall-clock gate; 3 s of silence at a 23 s lead)."""
        self.use_pool(*KOGR_POOL)
        for lead, before in ((23.0, 3.0), (15.0, 11.0)):
            with self.subTest(lead=lead):
                LogItem.objects.filter(playlist_log__hour=22).delete()
                PlaylistLog.objects.filter(hour=22).delete()
                LogItem.objects.filter(playlist_log__hour=21, origin=LogItem.ORIGIN_LIVE_FILL).delete()
                sim, m = self.simulate_eoh(26.0, lead=lead, lead_in=3.0)
                self.assert_clean_boundary(sim, m)
                print(f"\nS16 lead={lead} silence={m.silence} (r0135/ba3f8a4: {before}) next_offset={m.next_offset} "
                      f"aired_fill={m.aired_fill} rows={len(m.fill_rows)} workers={sim.workers}")
                self.assertEqual(m.aired_fill, [25.8], m.report)
                self.assertEqual((m.silence, len(m.fill_rows)), (0.0, 1), m.report)
                self.assertLessEqual(m.fill_overrun, 0.0, m.report)

    def test_s17_final_song_cut_short_with_nothing_queued(self):
        """Measured, not improved: the final song would have carried the hour (it ends 5 s after it) and is cut
        short with nothing queued, so the shortage appears only at that moment, on the reactive end-of-queue
        path. That path dispatches through _roll_over_to_next_hour and then runs r0135's _on_log_exhausted,
        which reloads the (not yet approved) next hour and clears the active queue, so any proposal --
        closing or ordinary -- is discarded as stale. The 120 s row shows the same for an ordinary fill."""
        self.use_pool(*KOGR_POOL)
        for cut_left, lead in ((27.0, 23.0), (27.0, 15.0), (120.0, 23.0)):
            with self.subTest(cut_left=cut_left, lead=lead):
                LogItem.objects.filter(playlist_log__hour=22).delete()
                PlaylistLog.objects.filter(hour=22).delete()
                LogItem.objects.filter(playlist_log__hour=21, origin=LogItem.ORIGIN_LIVE_FILL).delete()
                cut = TOH - timedelta(seconds=cut_left)

                def cut_short(sim, engine):
                    engine.decks["B"] = None
                    sim.call("eos", engine._start_next_track)
                sim, _m = self.simulate_eoh(-5.0, lead=lead, lead_in=cut_left + 30.0, events=[(cut, cut_short)])
                m = self.measure(sim, cut)
                self.assert_clean_boundary(sim, m)
                print(f"\nS17 cut_left={cut_left} lead={lead} silence={m.silence} next_offset={m.next_offset} "
                      f"workers={sim.workers} delivered={sim.delivered} rows={len(m.fill_rows)} aired={m.aired_fill}")
                self.assertEqual(len(m.fill_rows), len(m.aired_fill), m.report)   # no unaired old-hour row
                self.assertLessEqual(m.silence, cut_left - lead, m.report)        # never longer than waiting

    def test_gap_table_before_and_after(self):
        """Measured silence (s) / next-hour start offset (s) for a KOGR-shaped PSA pool."""
        self.use_pool(*KOGR_POOL)
        cells = []
        for deficit in (10.0, 17.0, 20.0, 25.0, 28.0, 30.0, 45.0, 120.0):
            for lead in (26.0, 23.0, 15.0, 8.0):
                LogItem.objects.filter(playlist_log__hour=22).delete()
                PlaylistLog.objects.filter(hour=22).delete()
                LogItem.objects.filter(playlist_log__hour=21, origin=LogItem.ORIGIN_LIVE_FILL).delete()
                random.seed(1)
                sim, m = self.simulate_eoh(deficit, lead=lead)
                cells.append((deficit, lead, m.silence, m.next_offset, m.aired_fill, m.fill_overrun))
                self.assert_clean_boundary(sim, m)
        print("\nGAP TABLE deficit_s lead_s silence_s next_offset_s aired_fill_s fill_overrun_s")
        for cell in cells:
            print("GAP %6.1f %5.1f %6.2f %7s %s %s" % cell)
        # With the ordinary approval lead (KOGR: 23-26 s) no end-of-hour silence remains at all.
        self.assertEqual([c for c in cells if c[1] >= 23.0 and c[2] > 0], [])


# --------------------------------------------------------------------------- unit-level ownership tests

class _UnitFixture(_RolloverFixture):

    def setUp(self):
        super().setUp()
        self.pool = self.use_pool(15.0, 29.0)

    use_pool = _EndOfHourFixture.use_pool

    def engine_with_final_song(self, *, remaining=90.0, deficit=25.0, now=None):
        """A live engine whose queue cursor is at the end while the final song has `remaining` seconds left."""
        now = now or TOH - timedelta(seconds=deficit + remaining)
        engine = _live_fill_engine()
        engine.current_log = self.old_log
        engine.log_items = self.reload_items(self.old_log)
        engine._queue_cursor = len(engine.log_items)
        engine._apply_poison_skip = lambda items: items
        engine.decks["B"].ends_at = now + timedelta(seconds=remaining)

        def runway():   # the leading (soonest-finishing) deck; the queue past the cursor is empty
            clock = eng_module.timezone.localtime()
            etas = [(d.ends_at - clock).total_seconds() for d in engine.decks.values() if d is not None]
            return max(0.0, min(etas)) if etas else 0.0
        engine._committed_future_runway_seconds = runway
        return engine, now, None

    def dispatch(self, engine, now):
        idle_calls = []
        with patch.object(eng_module.timezone, "localtime", return_value=now), \
                patch.object(eng_module.threading, "Thread", _SyncThread), \
                patch.object(log_builder, "_weighted_choice_by", _closest), \
                patch.object(eng_module.GLib, "idle_add", side_effect=lambda fn, *a: idle_calls.append((fn, a)) or 1):
            engine._last_live_extend_attempt = 0.0
            engine._try_extend_live_log_async()
        return idle_calls

    @staticmethod
    def deliver(call, when):
        fn, args = call
        with patch.object(eng_module.timezone, "localtime", return_value=when):
            return fn(*args)

    def fill_rows(self):
        return list(LogItem.objects.filter(playlist_log=self.old_log, origin=LogItem.ORIGIN_LIVE_FILL))

    def date_old_log(self, day, hour):
        """Give the active (old) log the wall-clock hour a test closes (the fixture's own is 2027-03-03 21:00)."""
        self.old_log.date, self.old_log.hour = day, hour
        self.old_log.save(update_fields=["date", "hour"])

    def idle_engine(self, left, *, now=None):
        """An idle engine (both decks empty) whose current-hour queue is exhausted `left` s before the hour."""
        engine, now, _ = self.engine_with_final_song(remaining=0.0, deficit=left, now=now)
        engine.decks["B"] = None
        return engine, now

    def adopt_next_hour(self, engine, when):
        """The real next-hour adoption (_advance_to_next_hour_log) of an approved 22:00 log; returns its item ids."""
        if not PlaylistLog.objects.filter(date=WEDNESDAY, hour=22).exists():
            self.make_log(22, self.next_rows, played=False)
        engine._next_hour_peek, engine._next_hour_peek_at, engine._next_hour_peek_key = None, 0.0, None
        engine._poison_skip_identities = []
        with patch.object(eng_module.timezone, "localtime", return_value=when):
            engine._advance_to_next_hour_log(WEDNESDAY, 22)
        self.assertEqual(engine.current_log.hour, 22)
        return [i.id for i in engine.log_items]

    def classify(self, engine, now):
        """Dispatch once; returns (worker kwargs or None when nothing was dispatched, idle callbacks)."""
        seen = {}
        real_worker = engine._live_fill_worker

        def worker(*args, **kwargs):
            seen.update(args=args, kwargs=kwargs)
            return real_worker(*args, **kwargs)
        engine._live_fill_worker = worker
        calls = self.dispatch(engine, now)
        return (seen.get("kwargs") if seen else None), seen.get("args"), calls


class ClosingFillOwnershipTests(_UnitFixture, TransactionTestCase):
    """Scenarios 10-13 and 15, GLib-thread database work, DST and the unchanged ordinary contracts."""

    def test_closing_proposal_is_delivered_and_installed_once(self):
        engine, now, _state = self.engine_with_final_song()
        calls = self.dispatch(engine, now)
        self.assertEqual(len(calls), 1, "a 25 s deficit was not dispatched")
        _fn, args = calls[0]
        self.assertEqual(len(args), 6)                    # the closing deadline rides along
        self.assertEqual(args[5], TOH)
        self.assertEqual([p["track"].id for p in args[2]], [self.pool[0].id])
        self.deliver(calls[0], now + timedelta(seconds=2))
        self.assertEqual([r.track_id for r in self.fill_rows()], [self.pool[0].id])
        engine._start_next_track.assert_not_called()       # the final song still owns its deck

    def test_s10_worker_completion_delayed_across_the_top_of_the_hour(self):
        engine, now, state = self.engine_with_final_song()
        calls = self.dispatch(engine, now)
        engine.decks["B"] = None                           # the final song ended meanwhile
        self.deliver(calls[0], TOH + timedelta(seconds=4))
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual(len(engine.log_items), 4)
        engine._start_next_track.assert_not_called()
        # Delivered with more than 30 s left, but an urgent item now holds the air until 10 s before the
        # hour (urgent items live outside log_items, so the queue-length check cannot see it): discarded.
        engine2, now2, _ = self.engine_with_final_song(remaining=40.0, deficit=25.0)
        calls2 = self.dispatch(engine2, now2)
        engine2.decks["B"] = None
        engine2.decks["A"] = SimpleNamespace(paused=False, finished=False, ends_at=TOH - timedelta(seconds=10))
        self.deliver(calls2[0], TOH - timedelta(seconds=40))
        self.assertEqual(self.fill_rows(), [])

    def test_delayed_delivery_while_idle(self):
        # A safe closing proposal delivered inside the final 30 s, while the current hour is still the active
        # queue and nothing else holds the air: installed and handed to playback at once.
        engine, now, _state = self.engine_with_final_song(remaining=40.0, deficit=29.0)
        calls = self.dispatch(engine, now)                 # 29 s deficit -> the 29 s PSA
        self.assertEqual([p["track"].id for p in calls[0][1][2]], [self.pool[1].id])
        engine.decks["B"] = None                           # the song was cut short (e.g. Start Next)
        self.deliver(calls[0], TOH - timedelta(seconds=30))
        self.assertEqual([r.track_id for r in self.fill_rows()], [self.pool[1].id])
        self.assertEqual([i.track_id for i in engine.log_items[4:]], [self.pool[1].id])
        engine._start_next_track.assert_called_once_with()
        LogItem.objects.filter(origin=LogItem.ORIGIN_LIVE_FILL).delete()
        # The same delivery after the next hour's queue was adopted: no row, no in-memory item, no start.
        engine2, now2, _ = self.engine_with_final_song(remaining=40.0, deficit=29.0)
        calls2 = self.dispatch(engine2, now2)
        engine2.decks["B"] = None
        adopted = self.adopt_next_hour(engine2, TOH - timedelta(seconds=31))
        self.deliver(calls2[0], TOH - timedelta(seconds=30))
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual([i.id for i in engine2.log_items], adopted)
        engine2._start_next_track.assert_not_called()
        # A delivery too late for the 29 s PSA to end by the hour: no row, no in-memory item, no start.
        engine3, now3, _ = self.engine_with_final_song(remaining=40.0, deficit=29.0)
        calls3 = self.dispatch(engine3, now3)
        engine3.decks["B"] = None
        self.deliver(calls3[0], TOH - timedelta(seconds=20))
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual(len(engine3.log_items), 4)
        engine3._start_next_track.assert_not_called()

    def test_s11_stale_callback_after_rollover(self):
        engine, now, _state = self.engine_with_final_song()
        calls = self.dispatch(engine, now)
        engine.current_log = PlaylistLog.objects.create(date=WEDNESDAY, hour=22, status="approved")
        engine.log_items = []
        self.deliver(calls[0], now + timedelta(seconds=1))
        self.assertEqual(self.fill_rows(), [])

    def test_s12_manual_mode_and_shutdown_during_an_in_progress_fill(self):
        engine, now, _state = self.engine_with_final_song()
        calls = self.dispatch(engine, now)
        engine.manual_mode = True
        engine.decks["B"] = None
        self.deliver(calls[0], now + timedelta(seconds=1))
        self.assertEqual(len(self.fill_rows()), 1)        # queued for the hour ...
        engine._start_next_track.assert_not_called()       # ... but Manual starts nothing
        LogItem.objects.filter(origin=LogItem.ORIGIN_LIVE_FILL).delete()
        engine2, now2, _ = self.engine_with_final_song()
        calls2 = self.dispatch(engine2, now2)
        engine2.running = False
        self.deliver(calls2[0], now2 + timedelta(seconds=1))
        self.assertEqual(self.fill_rows(), [])
        # A worker finishing during teardown schedules nothing at all.
        engine3, now3, _ = self.engine_with_final_song()
        engine3.running = False
        self.assertEqual(self.dispatch(engine3, now3), [])
        self.assertFalse(engine3._live_fill_in_progress)

    def test_s13_hour_refill_arbitration(self):
        # Observation-only: a coordinator exists but owns nothing -> the closing fill proceeds and its
        # pending-callback mark is cleared by the install.
        engine, now, _state = self.engine_with_final_song()
        engine._refill_coordinator = RefillCoordinator()
        calls = self.dispatch(engine, now)
        self.assertEqual(len(calls), 1)
        self.assertEqual(engine._refill_coordinator.legacy_callbacks_pending, {engine._live_fill_generation})
        self.deliver(calls[0], now + timedelta(seconds=1))
        self.assertEqual(len(self.fill_rows()), 1)
        self.assertEqual(engine._refill_coordinator.legacy_callbacks_pending, set())
        LogItem.objects.filter(origin=LogItem.ORIGIN_LIVE_FILL).delete()
        # A refill owner blocks dispatch outright ...
        engine2, now2, _ = self.engine_with_final_song()
        engine2._refill_coordinator = RefillCoordinator()
        engine2._refill_coordinator.refill_owner = object()
        self.assertEqual(self.dispatch(engine2, now2), [])
        self.assertFalse(engine2._live_fill_in_progress)
        # ... and an owner that appears between dispatch and install discards the proposal.
        engine3, now3, _ = self.engine_with_final_song()
        engine3._refill_coordinator = RefillCoordinator()
        calls3 = self.dispatch(engine3, now3)
        engine3._refill_coordinator.refill_owner = object()
        self.deliver(calls3[0], now3 + timedelta(seconds=1))
        self.assertEqual(self.fill_rows(), [])
        self.assertEqual(engine3._refill_coordinator.legacy_discards, 1)
        self.assertEqual(engine3._refill_coordinator.legacy_callbacks_pending, set())

    def test_s15_duplicate_callback_and_repeated_dispatch(self):
        engine, now, _state = self.engine_with_final_song()
        calls = self.dispatch(engine, now)
        self.deliver(calls[0], now + timedelta(seconds=1))
        self.deliver(calls[0], now + timedelta(seconds=2))   # the same callback again
        self.assertEqual(len(self.fill_rows()), 1)
        # A dispatch while a worker is in flight starts nothing.
        engine2, now2, _ = self.engine_with_final_song()
        engine2._live_fill_in_progress = True
        self.assertEqual(self.dispatch(engine2, now2), [])
        # A closing deficit nothing fits is attempted once per queue state, not on every throttle window.
        Track.objects.filter(id=self.pool[0].id).update(ready2air=False)    # only the 29 s PSA remains
        engine3, now3, _ = self.engine_with_final_song(deficit=25.0)
        workers = []
        real_worker = engine3._live_fill_worker
        engine3._live_fill_worker = lambda *a, **k: workers.append(a) or real_worker(*a, **k)
        for step in range(4):
            self.assertEqual(self.dispatch(engine3, now3 + timedelta(seconds=6 * step)), [])
        self.assertEqual(len(workers), 1)
        self.assertEqual(len(self.fill_rows()), 1)          # only the first engine's single install

    def test_closing_install_adds_no_glib_thread_queries(self):
        # Discarding a closing proposal for lack of time runs no query at all ...
        engine, now, _state = self.engine_with_final_song()
        calls = self.dispatch(engine, now)
        engine.decks["B"] = None
        with CaptureQueriesContext(connection) as discard_queries:
            self.deliver(calls[0], TOH + timedelta(seconds=1))
        self.assertEqual(len(discard_queries), 0, [q["sql"] for q in discard_queries])
        # ... and a successful closing install runs exactly the queries of an ordinary install of the same pick.
        engine2, now2, _ = self.engine_with_final_song()
        calls2 = self.dispatch(engine2, now2)
        fn, args = calls2[0]

        def fresh(picks):    # uncached Track instances, so both installs pay the same lazy loads
            return [dict(p, track=Track.objects.get(id=p["track"].id)) for p in picks]
        closing_args = (args[0], args[1], fresh(args[2])) + args[3:]
        ordinary_args = (args[0], args[1], fresh(args[2])) + args[3:5]
        with CaptureQueriesContext(connection) as closing_queries:
            self.deliver((fn, closing_args), now2 + timedelta(seconds=1))
        LogItem.objects.filter(origin=LogItem.ORIGIN_LIVE_FILL).delete()
        engine3, _now3, _ = self.engine_with_final_song()
        engine3._live_fill_generation = args[1]
        with CaptureQueriesContext(connection) as ordinary_queries:
            self.deliver((engine3._install_live_fill, ordinary_args), now2 + timedelta(seconds=1))
        self.assertEqual(len(ordinary_queries), len(closing_queries))

    def test_dst_fall_back_deadline_is_an_absolute_instant(self):
        chicago = ZoneInfo(settings.TIME_ZONE)
        fall_back = datetime(2026, 11, 1, 1, 58, 50, tzinfo=chicago)               # 01:58:50 CDT, fold 0
        self.date_old_log(fall_back.date(), 1)
        engine, _now, _state = self.engine_with_final_song(now=fall_back, remaining=45.0, deficit=25.0)
        calls = self.dispatch(engine, fall_back)
        self.assertEqual(calls[0][1][5], datetime(2026, 11, 1, 7, 0, tzinfo=ZoneInfo("UTC")))
        engine.decks["B"] = None
        repeated = datetime(2026, 11, 1, 1, 0, 5, fold=1, tzinfo=chicago)          # 01:00:05 CST, after the hour
        self.deliver(calls[0], repeated)
        self.assertEqual(self.fill_rows(), [])
        # Inside the repeated hour the 02:00 log installed early (01:59:30 CDT) is the active queue: a closing
        # fill at 01:59:35 CST never extends it.
        late_in_repeat = datetime(2026, 11, 1, 1, 59, 35, fold=1, tzinfo=chicago)
        self.date_old_log(late_in_repeat.date(), 2)
        engine2, _ = self.idle_engine(25.0, now=late_in_repeat)
        self.assertEqual(self.dispatch(engine2, late_in_repeat), [])
        self.assertFalse(engine2._live_fill_in_progress)
        # With the repeated hour's own log active, its deadline is 02:00 CST (08:00 UTC), not 02:00 CDT.
        self.date_old_log(late_in_repeat.date(), 1)
        engine3, _ = self.idle_engine(25.0, now=late_in_repeat)
        calls3 = self.dispatch(engine3, late_in_repeat)
        self.assertEqual(calls3[0][1][5], datetime(2026, 11, 1, 8, 0, tzinfo=ZoneInfo("UTC")))

    def test_ordinary_dispatch_contract_is_unchanged(self):
        engine, now, _state = self.engine_with_final_song(remaining=60.0, deficit=120.0)
        captured = {}
        with patch.object(eng_module.timezone, "localtime", return_value=now), \
                patch.object(engine, "_live_fill_worker", side_effect=lambda *a, **k: captured.update(args=a, kwargs=k)), \
                patch.object(eng_module.threading, "Thread", _SyncThread):
            engine._try_extend_live_log_async()
        self.assertEqual(len(captured["args"]), 8)
        self.assertEqual(captured["kwargs"], {})
        self.assertEqual(captured["args"][7], 120.0)
        engine._live_fill_in_progress = False              # the stand-in worker did not clear it
        calls = self.dispatch(engine, now + timedelta(seconds=6))
        self.assertEqual(len(calls[0][1]), 5)              # the ordinary callback shape


class ClosingFillTimingTests(_UnitFixture, TransactionTestCase):
    """Codex timing findings on ba3f8a4: (A) the installer's final-30 s gate, (B) the dispatcher's final-30 s
    gate and (C) the whole-second classification of a closing deficit."""

    def test_a_closing_callback_at_29_to_30s_before_and_after_next_hour_adoption(self):
        for left in (30.0, 29.5, 29.0):
            with self.subTest(left=left):
                LogItem.objects.filter(origin=LogItem.ORIGIN_LIVE_FILL).delete()
                PlaylistLog.objects.filter(hour=22).delete()
                engine, now, _ = self.engine_with_final_song(remaining=40.0, deficit=29.0)
                calls = self.dispatch(engine, now)          # the 29 s PSA, deadline TOH
                engine.decks["B"] = None
                self.deliver(calls[0], TOH - timedelta(seconds=left))
                self.assertEqual([r.track_id for r in self.fill_rows()], [self.pool[1].id])
                self.assertEqual(engine._start_next_track.call_count, 1)
                # The identical callback after the next hour's queue is adopted changes nothing.
                LogItem.objects.filter(origin=LogItem.ORIGIN_LIVE_FILL).delete()
                engine2, now2, _ = self.engine_with_final_song(remaining=40.0, deficit=29.0)
                calls2 = self.dispatch(engine2, now2)
                engine2.decks["B"] = None
                adopted = self.adopt_next_hour(engine2, TOH - timedelta(seconds=left + 0.5))
                self.deliver(calls2[0], TOH - timedelta(seconds=left))
                self.assertEqual(self.fill_rows(), [])
                self.assertEqual([i.id for i in engine2.log_items], adopted)
                engine2._start_next_track.assert_not_called()

    def test_b_reactive_closing_dispatch_with_21_to_30s_of_actual_shortage(self):
        for left in (30.0, 29.5, 25.0, 21.0):
            with self.subTest(left=left):
                LogItem.objects.filter(origin=LogItem.ORIGIN_LIVE_FILL).delete()
                engine, now = self.idle_engine(left)
                kwargs, args, calls = self.classify(engine, now)
                self.assertEqual(kwargs, {"closing_until": TOH})
                self.assertEqual(args[7], left)            # the exact shortage
                self.assertEqual(len(calls), 1)
                picks = calls[0][1][2]
                self.assertLessEqual(sum(effective_airtime_seconds(p["track"]) for p in picks), left)
                # A second dispatch inside the 5 s throttle, or while a worker is in flight, starts nothing.
                started = []
                with patch.object(eng_module.timezone, "localtime", return_value=now + timedelta(seconds=1)), \
                        patch.object(eng_module.threading, "Thread", side_effect=lambda **k: started.append(k)):
                    engine._try_extend_live_log_async()
                self.assertEqual(started, [])
                engine._live_fill_in_progress = True
                self.assertEqual(self.dispatch(engine, now + timedelta(seconds=2)), [])
                engine._live_fill_in_progress = False
                self.deliver(calls[0], now + timedelta(milliseconds=500))
                self.assertEqual([r.track_id for r in self.fill_rows()], [p["track"].id for p in picks])
                engine._start_next_track.assert_called_once_with()
        # The adopted next hour is never extended by an obsolete current-hour fill -- even when (as this
        # runway stand-in reports, like an adopted queue that is almost empty) it commits no runway at all.
        engine, now = self.idle_engine(25.0)
        self.adopt_next_hour(engine, now)
        before = len(engine.log_items)
        kwargs, _args, calls = self.classify(engine, now)
        self.assertEqual((kwargs, calls, len(engine.log_items)), (None, [], before))
        self.assertFalse(LogItem.objects.filter(playlist_log__hour=22, origin=LogItem.ORIGIN_LIVE_FILL).exists())
        # A causal refill owner still blocks a final-30 s closing dispatch outright.
        engine, now = self.idle_engine(25.0)
        engine._refill_coordinator = RefillCoordinator()
        engine._refill_coordinator.refill_owner = object()
        self.assertEqual(self.classify(engine, now)[0::2], (None, []))
        self.assertFalse(engine._live_fill_in_progress)

    def test_c_classification_thresholds(self):
        """remaining wall time has a fractional second; runway 40.5 s; the deficit decides alone."""
        cases = (
            (20.0, None),            # the floor itself: left to the next-hour lookahead
            (20.25, "closing"),
            (29.75, "closing"),      # Codex: 70.25 s left - 40.50 s runway (whole-second reading: 30.50)
            (30.0, "closing"),       # the margin itself
            (30.25, "ordinary"),
            (31.0, "ordinary"),
        )
        for deficit, expected in cases:
            with self.subTest(deficit=deficit):
                engine, now, _ = self.engine_with_final_song(remaining=40.5, deficit=deficit)
                kwargs, args, _calls = self.classify(engine, now)
                kind = None if kwargs is None else ("closing" if kwargs.get("closing_until") == TOH else "ordinary")
                self.assertEqual(kind, expected)
                if kwargs is not None:
                    self.assertAlmostEqual(args[7], deficit, places=6)

    def test_c_subsecond_29_75s_shortage_rejects_a_too_long_fallback(self):
        # Only items longer than the 29.75 s shortage are Ready to Air: the ordinary fill (30 s tolerance)
        # would take one and run past the hour; the closing fill takes none and does not retry.
        self.use_pool(30.0, 45.0)                          # replaces the 15 s / 29 s pool
        engine, now, _ = self.engine_with_final_song(remaining=40.5, deficit=29.75)
        self.assertEqual(now.microsecond, 750000)
        kwargs, _args, calls = self.classify(engine, now)
        proposed = [effective_airtime_seconds(p["track"]) for c in calls for p in c[1][2]]
        print(f"\nC29.75 classified={'closing' if kwargs and kwargs.get('closing_until') else 'ordinary'} "
              f"proposed={proposed} would end {40.5 + sum(proposed) - 70.25:+.2f}s from the hour")
        self.assertEqual(kwargs, {"closing_until": TOH})
        self.assertEqual(calls, [])
        self.assertEqual(getattr(engine, "_live_fill_closing_no_fit", None), (self.old_log.id, 4))
        self.assertEqual(self.fill_rows(), [])
        # With a fitting item Ready to Air the same shortage is closed, ending before the hour.
        fits = self.use_pool(29.0)
        engine2, now2, _ = self.engine_with_final_song(remaining=40.5, deficit=29.75)
        _kwargs, _args, calls2 = self.classify(engine2, now2)
        self.assertEqual([p["track"].id for p in calls2[0][1][2]], [fits[0].id])
        self.deliver(calls2[0], now2 + timedelta(seconds=1))
        self.assertEqual([r.track_id for r in self.fill_rows()], [fits[0].id])

    def test_midnight_deadline_and_dispatch_inside_the_final_30s(self):
        chicago = ZoneInfo(settings.TIME_ZONE)
        before_midnight = datetime(2026, 11, 11, 23, 59, 35, 500000, tzinfo=chicago)
        self.date_old_log(before_midnight.date(), 23)
        engine, now = self.idle_engine(24.5, now=before_midnight)
        kwargs, args, calls = self.classify(engine, now)
        self.assertEqual(kwargs, {"closing_until": datetime(2026, 11, 12, 0, 0, tzinfo=chicago)})
        self.assertEqual(args[5], datetime(2026, 11, 11, 23, 0, tzinfo=chicago))   # the hour being closed
        self.deliver(calls[0], datetime(2026, 11, 12, 0, 0, 0, 100000, tzinfo=chicago))   # just past midnight
        self.assertEqual(self.fill_rows(), [])
        engine._start_next_track.assert_not_called()


class SchedulerFitSemanticsUnchangedTests(_UnitFixture, TransactionTestCase):
    """Scheduled log generation keeps DURATION_FIT_MARGIN: no closing behaviour outside the live dispatcher."""

    def test_default_fill_remaining_hour_stops_within_the_margin(self):
        for remaining in (30.0, 25.0, 17.0):
            picks, accumulated = fill_remaining_hour([], 3600.0 - remaining, at(21, 0))
            self.assertEqual((picks, accumulated), ([], 3600.0 - remaining))

    def test_default_best_fit_still_tolerates_overshoot_within_the_margin(self):
        with patch.object(log_builder, "_weighted_choice_by", _closest):
            track = pick_track(self.psa, set(), set(), 0, 0, at(21, 0), remaining_seconds=20.0, force_fit_mode=True)
        self.assertEqual(track.id, self.pool[0].id)        # 15 s (5 s under) is closer than 29 s (9 s over)
        Track.objects.filter(id=self.pool[0].id).update(ready2air=False)
        with patch.object(log_builder, "_weighted_choice_by", _closest):
            track = pick_track(self.psa, set(), set(), 0, 0, at(21, 0), remaining_seconds=20.0, force_fit_mode=True)
        self.assertEqual(track.id, self.pool[1].id)        # a 9 s overshoot is still acceptable here

    def test_closing_mode_never_overshoots_and_keeps_strict_separation(self):
        with patch.object(log_builder, "_weighted_choice_by", _closest):
            picks, _ = fill_remaining_hour([], 0.0, at(21, 0), target_duration_seconds=20.0, closing_fill=True)
            self.assertEqual([p["track"].id for p in picks], [self.pool[0].id])
            picks, _ = fill_remaining_hour([], 0.0, at(21, 0), target_duration_seconds=14.0, closing_fill=True)
            self.assertEqual(picks, [])
            # The only fitting item is already in this hour: no repeat, no loosening.
            history = [{"track": self.pool[0], "category": self.psa}]
            picks, _ = fill_remaining_hour(history, 0.0, at(21, 0), target_duration_seconds=20.0, closing_fill=True)
            self.assertEqual(picks[1:], [])
