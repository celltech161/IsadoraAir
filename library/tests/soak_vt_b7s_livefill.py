"""Roadmap 2.25 B7 supplemental -- normal live-fill soak (MEASUREMENT; no
``test_`` prefix; run explicitly through the guarded runner):

    manage.py test library.tests.soak_vt_b7s_livefill.LiveFillSoak.test_soak_4h
    manage.py test library.tests.soak_vt_b7s_livefill.LiveFillSoak.test_soak_smoke

Real wall-clock hours. Every approved hour log is deliberately SHORT, so the
engine's own NORMAL live fill (``_try_extend_live_log_async`` ->
``_live_fill_worker`` -> ``fill_remaining_hour`` -> ``_install_live_fill``)
must extend it; frequent scripted Start Next / Eject consume runway and
re-open deficits, so the fill is decided many times per hour. Hour adoption
at the real top of the hour, the queue reload, the Hour Refill coordinator
(installed policy untouched: observation only) and the VT transition
lifecycle are the engine's own. PostgreSQL accounting, real VoiceTrack rows
and the real lookup worker.

Scripted on top: Start Next, Eject, Manual/Auto, studio mic, Remote DJ gate
(stand-in: the WebRTC media path has its own suite), slow/failing lookups,
failed VT preparation (readiness stall), urgent insertion, occurrence
replacement (Swap) while a live-fill proposal is in flight, and a live-fill
proposal held in flight across the top of the hour.

Evidence: every claimed occurrence is traced to a terminal disposition;
silence is classified bin by bin against operator holds and bounded
post-command start latency; memory is sampled (RSS, smaps_rollup, malloc
arenas, tracemalloc, gc type counts, engine container sizes) with the
harness keeping no unbounded history (everything is streamed to stdout).
"""

from __future__ import annotations

import ctypes
import gc
import json
import random
import time
import tracemalloc
from collections import Counter, OrderedDict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.services import hour_refill_projection, vt_transition
from library.tests._transport_harness import start_next_command
from library.tests._vt_b0_lab import NS, GLib, Gst, Lab, pump
from library.tests._vt_integration_lab import vt_segments

SEED = 4711
SILENCE_RMS = 0.003
BIN_S = 0.010
START_LATENCY_S = 1.0          # bounded post-command / post-release start latency
LOOKAHEAD_HOURS = 6


def emit(kind, **row):
    print(f"LF-{kind} " + json.dumps(row, sort_keys=True, default=str), flush=True)


class _MallInfo2(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
                                               "fsmblks", "uordblks", "fordblks", "keepcost")]


def mallinfo():
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.mallinfo2.restype = _MallInfo2
        m = libc.mallinfo2()
        return {"arena_mb": round(m.arena / 2**20, 1), "in_use_mb": round(m.uordblks / 2**20, 1),
                "free_mb": round(m.fordblks / 2**20, 1), "mmap_mb": round(m.hblkhd / 2**20, 1)}
    except Exception as exc:
        return {"error": repr(exc)}


def smaps():
    out = {}
    try:
        for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
            key, _sep, rest = line.partition(":")
            if key in ("Rss", "Pss", "Private_Clean", "Private_Dirty", "Anonymous"):
                out[key.lower() + "_mb"] = round(int(rest.split()[0]) / 1024, 1)
    except OSError:
        pass
    return out


class Library:
    def __init__(self, case, lab, rng):
        from library.models import (
            Artist, Category, CategoryKind, LogFillConfig, RecencyConfig, VoiceTrack, VoiceTrackConfig,
        )
        self.case, self.lab, self.rng = case, lab, rng
        VoiceTrackConfig.objects.update_or_create(pk=1, defaults={"program_duck_db": -6.0, "duck_ramp_ms": 120,
                                                                 "min_gap_ms": 200})
        # Test database only: separations scaled to the soak's short songs.
        recency = RecencyConfig.load()
        # A 30-track fill pool and ~4-minute separation: each NORMAL live-fill
        # proposal can cover only a few minutes, so the engine decides a fresh
        # fill every couple of minutes (many real decisions per hour) without
        # starving the pool. Admin configuration only; no code path changes.
        recency.artist_separation = 0.05
        recency.title_separation = 0.07
        recency.save()
        music = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})[0]
        self.sched = Category.objects.create(code="LFSCHED", name="LF scheduled", kind=music)
        self.fill = Category.objects.create(code="LFFILL", name="LF fill", kind=music)
        fill_cfg = LogFillConfig.load()
        fill_cfg.strategy = "fixed_category"
        fill_cfg.fallback_category = self.fill
        fill_cfg.save()
        self.VoiceTrack = VoiceTrack
        self.Artist = Artist
        self.scheduled_tracks = [self.track(f"lf-sched-{n}", self.sched, n) for n in range(40)]
        self.fill_tracks = [self.track(f"lf-fill-{n}", self.fill, 1000 + n) for n in range(30)]
        self.urgent = self.track("lf-urgent", self.sched, 9999, fixed=(3.0, 300))

    def track(self, name, category, n, fixed=None):
        from library.models import Track
        rng = self.rng
        artist = self.Artist.objects.create(name=f"LF Artist {n}")
        if fixed:
            length, freq = fixed
            segs, fields = [(length, freq)], {}
        else:
            length = rng.choice((8.0, 9.5, 11.0, 12.5, 14.0))
            cue = rng.choice((0.0, 0.15, 0.25))
            freq = 500 + (n % 7) * 100
            segs = ([(cue, 0)] if cue else []) + [(length - cue - 1.0, freq), (1.0, 1400)]
            fields = {"cue_in_seconds": cue, "next_start_seconds": round(length - rng.uniform(0.6, 1.2), 3)}
            if n % 9:
                fields["intro_until_seconds"] = round(rng.uniform(1.5, 4.5), 3)
            if n % 4 == 0:
                fields["outro_starts_seconds"] = round(length - rng.uniform(2.0, 3.5), 3)
        path = self.lab.media(name, segs, fmt="flac")
        t = Track.objects.create(filepath=str(path), filename=Path(path).name, title=name, artist=artist,
                                 category=category, ready2air=True, duration_seconds=round(sum(s for s, _ in segs), 6),
                                 **fields)
        if not fixed:
            if n % 2 == 0:       # intro VTs: short (U = 0) and long (underlap)
                seconds = round(rng.choice((1.0, 2.0, 5.5, 7.0)), 3)
                self.voicetrack(t, "intro", seconds, corrupt=(n % 23 == 0))
            if n % 3 == 0:
                self.voicetrack(t, "outro", round(rng.uniform(1.0, 3.0), 3), corrupt=(n % 29 == 0))
        return t

    def voicetrack(self, track, position, seconds, corrupt=False):
        if corrupt:
            path = self.lab.root / f"lf-corrupt-{track.pk}-{position}.flac"
            path.write_bytes(b"\x00\xffnot audio" * 2048)
        else:
            path = self.lab.media(f"lf-vt-{track.pk}-{position}-{seconds}",
                                  vt_segments(seconds, 1000 if position == "intro" else 900), fmt="flac")
        self.VoiceTrack.objects.create(track=track, position=position, filepath=str(path),
                                       duration_seconds=seconds, gain_db=0.0)

    def hour_log(self, hour_start, content_seconds):
        """An approved, deliberately short hour: `content_seconds` of
        scheduled items from the top of the hour; live fill does the rest."""
        from library.models import LogItem, PlaylistLog
        local = timezone.localtime(hour_start)
        log, _ = PlaylistLog.objects.update_or_create(date=local.date(), hour=local.hour,
                                                      defaults={"status": "approved"})
        LogItem.objects.filter(playlist_log=log).delete()
        total, pos = 0.0, 0
        while total < content_seconds:
            t = self.rng.choice(self.scheduled_tracks)
            pos += 1
            LogItem.objects.create(playlist_log=log, position=pos, scheduled_time=hour_start + timedelta(seconds=total),
                                   track=t, track_title=t.title, track_artist=t.artist.name, category=t.category)
            total += t.duration_seconds
        return log


class LiveFillSoak(TransactionTestCase):
    def setUp(self):
        self.counters = Counter()
        self.claims = {}            # log_item_id -> {"claims": n, "first": t}
        self.removals = {}          # log_item_id -> last removal facts
        self.ejected = set()
        self.holds = []             # [start, end] operator hold intervals (running seconds)
        self.commands = []          # running seconds of Eject / Start Next / hold release
        self.unexplained_bins = 0
        self.silence_runs = []
        self.silence_start = None
        self.harness_errors = []
        self.exhaustion_gaps = []

    # -- engine -------------------------------------------------------------------
    def build(self):
        lab = Lab(self)
        engine = lab.engine
        for name in ("_claim_playback_occurrence", "_schedule_occurrence_air_start_from_probe",
                     "_persist_deck_duration", "_roll_over_to_next_hour", "_peek_next_hour",
                     "_try_extend_live_log_async", "_on_log_exhausted"):
            engine.__dict__.pop(name, None)
        engine._write_now_playing = lambda _track: None
        engine._write_rbds_category_state = lambda _track: None
        for name in ("_check_commands", "_sweep_active_message_queue"):
            setattr(engine, name, lambda *_a, **_k: None)
        engine._vtt_lookup_inline = False
        self.addCleanup(lambda: engine.__dict__.get("_vtt_lookup_service") and engine._vtt_lookup_service.stop(2.0))
        engine._refill_coordinator = eng_module.RefillCoordinator()
        engine._cache_warm_item_id = None
        for attr, value in (("_live_fill_in_progress", False), ("_live_fill_generation", 0),
                            ("_last_live_extend_attempt", 0.0), ("_last_queue_reload", 0.0),
                            ("_next_hour_peek", None), ("_next_hour_peek_at", 0.0), ("_next_hour_peek_key", None)):
            if not hasattr(engine, attr):
                setattr(engine, attr, value)
        cfg = SimpleNamespace(ptt_auto_manual_enabled=True, enabled=False, duck_level_db=-10.0)
        engine.__dict__.setdefault("_duck_ramp_source_id", None)
        mocked = patch.object(eng_module.DuckingConfig, "load", new=lambda: cfg)
        mocked.start()
        self.addCleanup(mocked.stop)
        gate = Gst.ElementFactory.make("volume", None)
        gate.set_property("volume", 0.0)
        session = SimpleNamespace(slot_id=0, gate_desired=False, connection_attempt=None, emergency_latch=None)
        engine.dj_slots = [SimpleNamespace(remote_gate=gate, session=session)]
        engine.remote_dj_session = session
        self.dj = session
        return lab

    def instrument(self, engine):
        lab = self.lab
        claim, remove, eject = engine._claim_playback_occurrence, engine._remove_deck, engine._eject_deck
        dispatch, worker, install = (engine._try_extend_live_log_async, engine._live_fill_worker,
                                     engine._install_live_fill_checked)
        finish = engine._vtt_finish
        set_manual = engine._set_manual_mode

        def claimed(item):
            out = claim(item)
            row = self.claims.setdefault(item.id, {"claims": 0, "first": round(lab.now(), 3)})
            row["claims"] += 1
            return out

        def removed(deck, *, occurrence_terminal=True, termination_reason=None, defer_null=False):
            if getattr(deck, "log_item", None) is not None:
                import inspect
                callers = [f.function for f in inspect.stack()[1:6]]
                self.removals[deck.log_item.id] = {
                    "t": round(lab.now(), 3), "generation": deck.generation, "slot": deck.slot,
                    "state": getattr(deck, "admission_state", None),
                    "aired": bool(getattr(deck, "air_start_dispatched", False)),
                    "terminal": occurrence_terminal, "reason": termination_reason,
                    "finished": bool(getattr(deck, "finished", False)),
                    "start_failed": bool(getattr(deck, "start_failed", False)),
                    "media_buffers": getattr(deck, "media_buffer_count", None),
                    "callers": callers, "track_id": getattr(deck.log_item, "track_id", None)}
            return remove(deck, occurrence_terminal=occurrence_terminal, termination_reason=termination_reason,
                          defer_null=defer_null)

        def ejected(slot, *args, **kwargs):
            deck = engine.decks.get(slot)
            if deck is not None:
                self.ejected.add(deck.log_item.id)
            self.commands.append(lab.now())
            return eject(slot, *args, **kwargs)

        def dispatched():
            before = engine._live_fill_generation
            if engine._queue_cursor >= len(engine.log_items) and not any(
                    d is not None and not d.finished for d in engine.decks.values()):
                # Nothing left to play and no deck: the known late-proposal gap
                # (a fill decided only at exhaustion) until a fill installs.
                if not self.exhaustion_gaps or self.exhaustion_gaps[-1][1] is not None:
                    self.exhaustion_gaps.append([lab.now(), None])
            out = dispatch()
            if engine._live_fill_generation != before:
                self.counters["lf_dispatch"] += 1
                state = engine._vtt_observed_phase()
                self.counters[f"lf_dispatch_vt:{state}"] += 1
                emit("DISPATCH", t=round(lab.now(), 2), generation=engine._live_fill_generation, vt_phase=state,
                     queue=len(engine.log_items), cursor=engine._queue_cursor)
            return out

        def working(log_id, generation, *args, **kwargs):
            delay = self.worker_delays.pop(generation, 0.0)
            if delay:
                time.sleep(delay)
            self.counters["lf_worker"] += 1
            return worker(log_id, generation, *args, **kwargs)

        def installing(log_id, generation, new_picks, *args, **kwargs):
            before = len(engine.log_items)
            result = install(log_id, generation, new_picks, *args, **kwargs)   # a GLib callback: always False
            ok = len(engine.log_items) > before
            if ok and self.exhaustion_gaps and self.exhaustion_gaps[-1][1] is None:
                self.exhaustion_gaps[-1][1] = lab.now() + START_LATENCY_S
            self.counters["lf_install_ok" if ok else "lf_install_rejected"] += 1
            if ok:
                self.counters["lf_items_installed"] += len(engine.log_items) - before
            emit("INSTALL", t=round(lab.now(), 2), generation=generation, ok=bool(ok), picks=len(new_picks or []),
                 log_id=log_id, closing=kwargs.get("closing_until") is not None)
            return result

        def finished(record, reason):
            if not record.done:
                for side in (vt_transition.OUTRO, vt_transition.INTRO):
                    fire = record.outro_fire if side == vt_transition.OUTRO else record.intro_fire
                    status = "none" if fire is None else str(fire.state)
                    self.counters[f"vt:{side}:{status}"] += 1
                    why = record.outro_reason if side == vt_transition.OUTRO else record.intro_reason
                    if why:
                        self.counters[f"vt_reason:{side}:{why}"] += 1
            return finish(record, reason)

        def manual(active, _from_mic_release=False):
            was = engine.manual_mode
            out = set_manual(active, _from_mic_release=_from_mic_release)
            self.track_hold(was, engine.manual_mode)
            return out

        engine._claim_playback_occurrence = claimed
        engine._remove_deck = removed
        engine._eject_deck = ejected
        engine._try_extend_live_log_async = dispatched
        engine._live_fill_worker = working
        engine._install_live_fill_checked = installing
        engine._vtt_finish = finished
        engine._set_manual_mode = manual
        mic_hold = engine._apply_mic_mode_hold

        def mic_hold_tracked():
            was = engine.manual_mode
            out = mic_hold()
            self.track_hold(was, engine.manual_mode)
            return out
        engine._apply_mic_mode_hold = mic_hold_tracked

    def track_hold(self, was, now):
        t = self.lab.now()
        if now and not was:
            self.holds.append([t, None])
        elif was and not now and self.holds and self.holds[-1][1] is None:
            self.holds[-1][1] = t
            self.commands.append(t)

    # -- scripted actions ------------------------------------------------------------
    def guarded(self, name, fn):
        def run():
            try:
                fn()
            except Exception as exc:
                self.harness_errors.append(f"{name}: {exc!r}")
                emit("HARNESS-ERROR", name=name, error=repr(exc))
            return True
        return run

    def act_start_next(self):
        engine = self.lab.engine
        if engine.__dict__.get("_start_next_active") is None and engine._live_decks():
            self.commands.append(self.lab.now())
            engine._dispatch_engine_command(start_next_command(engine, fade_ms=500))
            self.counters["act:start_next"] += 1

    def act_eject(self):
        engine = self.lab.engine
        live = engine._live_decks()
        if live:
            engine._eject_deck(self.rng.choice(live).slot)
            self.counters["act:eject"] += 1

    def act_manual(self):
        engine = self.lab.engine
        engine._set_manual_mode(True)
        self.counters["act:manual"] += 1
        GLib.timeout_add(6000, lambda: (self.guarded("manual_off", lambda: engine._set_manual_mode(False))(), False)[1])

    def act_mic(self):
        engine = self.lab.engine
        engine.mic_live = True
        engine._apply_mic_mode_hold()
        self.counters["act:mic"] += 1

        def off():
            engine.mic_live = False
            engine._apply_mic_mode_hold()
        GLib.timeout_add(4000, lambda: (self.guarded("mic_off", off)(), False)[1])

    def act_remote(self):
        engine = self.lab.engine
        engine._remote_dj_set_gate(True)
        self.counters["act:remote_gate"] += 1

        def reconnect():
            engine._remote_dj_set_gate(False)
            engine.remote_dj_session = None
            engine._apply_mic_mode_hold()
            engine.remote_dj_session = self.dj
            engine._apply_mic_mode_hold()
        GLib.timeout_add(3000, lambda: (self.guarded("remote_reconnect", reconnect)(), False)[1])

    def act_lookup_trouble(self):
        service = self.lab.engine._vtt_lookups()
        resolve = service._resolve
        mode = {"p": "slow"}

        def troubled(key, payload):
            if mode["p"] == "slow":
                time.sleep(1.5)
            elif mode["p"] == "fail":
                raise RuntimeError("soak: database unavailable")
            return resolve(key, payload)
        service._resolve = troubled
        self.counters["act:lookup_trouble"] += 1
        GLib.timeout_add(30000, lambda: (mode.update(p="fail"), False)[1])

        def heal():
            mode["p"] = "ok"
            service._resolve = resolve
            return False
        GLib.timeout_add(50000, heal)

    def act_stall_next_readiness(self):
        engine = self.lab.engine
        real = engine.__class__._vt2_on_scheduled_deck_ready.__get__(engine)
        dropped = []

        def stall(slot, generation):
            if not dropped:
                dropped.append(1)
                self.counters["act:readiness_stall"] += 1
                engine.__dict__.pop("_vt2_on_scheduled_deck_ready", None)
                return False
            return real(slot, generation)
        engine._vt2_on_scheduled_deck_ready = stall

    def act_urgent(self):
        from library.models import LogItem
        engine = self.lab.engine
        if engine.current_log is None:
            return
        t = self.library.urgent
        pos = (engine.log_items[-1].position + 1000) if engine.log_items else 100000
        row = LogItem.objects.create(playlist_log=engine.current_log, position=pos, scheduled_time=timezone.now(),
                                     track=t, track_title=t.title, track_artist=t.artist.name, category=t.category)
        item = LogItem.objects.select_related("track", "category").get(pk=row.pk)
        engine.log_items.insert(engine._queue_cursor, item)
        engine._vtt_mark_urgent(item)
        engine._vtt_queue_changed("urgent_insert")
        self.counters["act:urgent"] += 1

    def act_swap_during_proposal(self):
        """Occurrence replacement while a live-fill proposal is in flight: the
        next worker is delayed and a queued (unclaimed) item's Track is swapped
        in the database meanwhile; the installer must reject the stale proposal."""
        from library.models import LogItem
        engine = self.lab.engine
        gen = engine._live_fill_generation + 1
        self.worker_delays[gen] = 2.5

        def swap():
            if engine._live_fill_generation != gen:
                return False
            queued = [i for i in engine.log_items[engine._queue_cursor:] if i.playback_claimed_at is None]
            if not queued:
                return False
            item = queued[-1]
            t = self.rng.choice(self.library.scheduled_tracks)
            LogItem.objects.filter(pk=item.pk).update(track=t, track_title=t.title)
            item.track, item.track_id, item.track_title = t, t.pk, t.title
            self.counters["act:swap_in_flight"] += 1
            return False

        def poll():
            if engine._live_fill_generation == gen:
                GLib.timeout_add(300, swap)
                return False
            return gen > engine._live_fill_generation and self.alive
        GLib.timeout_add(200, poll)

    def act_rollover_in_flight(self):
        """Keep the next proposal in flight across the top of the hour."""
        now = timezone.localtime()
        left = 3600 - (now.minute * 60 + now.second)
        hour = (now.date(), now.hour)
        if 20 < left < 240 and self.__dict__.get("_rollover_hour") != hour:
            self._rollover_hour = hour             # once per real hour
            self.worker_delays[self.lab.engine._live_fill_generation + 1] = left + 5.0
            self.counters["act:rollover_in_flight"] += 1

    def act_trim_queue(self):
        """An operator deletes the upcoming, unclaimed items of the current
        hour in the log editor (database); the engine's own 3-second queue
        reload adopts the shorter queue, and the next exhaustion makes a fresh
        NORMAL live-fill decision."""
        from library.models import LogItem
        engine = self.lab.engine
        if engine.current_log is None:
            return
        keep = {i.id for i in engine.log_items[:engine._queue_cursor + 2]}
        doomed = LogItem.objects.filter(playlist_log_id=engine.current_log.id, playback_claimed_at__isnull=True,
                                        played_at__isnull=True).exclude(id__in=keep)
        n = doomed.count()
        if n:
            doomed.delete()
            self.counters["act:trim_queue"] += 1
            self.counters["trimmed_items"] += n

    def act_observe(self):
        window = hour_refill_projection.capture_playback_window(self.lab.engine)
        self.counters[f"hr_phase:{getattr(window, 'vt_phase', None)}"] += 1

    # -- measurements ----------------------------------------------------------------------
    def in_hold_or_latency(self, t):
        for start, end in self.exhaustion_gaps:
            if start <= t <= (end if end is not None else float("inf")):
                self.counters["silence_bins_late_proposal"] += 1
                return True
        for start, end in self.holds:
            if start <= t <= (end if end is not None else float("inf")):
                return True
        return any(c <= t <= c + START_LATENCY_S for c in self.commands)

    def analyse_audio(self, flush=False):
        bufs = self.lab.h.program_buffers
        keep_after = self.lab.now() - (0.0 if flush else 3.0)
        cut = 0
        while cut < len(bufs) and bufs[cut][0] < keep_after:
            cut += 1
        chunk = bufs[:cut]
        del bufs[:cut]
        window = int(self.lab.rate * BIN_S)
        for running, samples in chunk:
            for i in range(0, len(samples) - window + 1, window):
                seg = samples[i:i + window]
                silent = (sum(v * v for v in seg) / len(seg)) ** 0.5 < SILENCE_RMS
                t = running + i / self.lab.rate
                if silent:
                    if self.silence_start is None:
                        self.silence_start, self.silence_unexplained = t, 0
                    if not self.in_hold_or_latency(t):
                        self.silence_unexplained += 1
                elif self.silence_start is not None:
                    length = t - self.silence_start
                    if length >= 0.5:
                        row = {"start": round(self.silence_start, 2), "seconds": round(length, 2),
                               "unexplained_s": round(self.silence_unexplained * BIN_S, 2)}
                        if row["unexplained_s"] > 0:
                            self.silence_runs.append(row)
                        emit("SILENCE", **row)
                    self.unexplained_bins += self.silence_unexplained if length >= 0.5 else 0
                    self.silence_start = None

    def containers(self, engine):
        sizes = {}
        for name, value in engine.__dict__.items():
            if isinstance(value, (dict, list, set, tuple, OrderedDict)) or hasattr(value, "__len__") and \
                    not isinstance(value, (str, bytes)):
                try:
                    n = len(value)
                except Exception:
                    continue
                if n >= 8:
                    sizes[name] = n
        return sizes

    def sample(self):
        engine = self.lab.engine
        self.analyse_audio()
        res = self.lab.resources()
        status = Path("/proc/self/status").read_text()
        rss = int(next(line.split()[1] for line in status.splitlines() if line.startswith("VmRSS"))) / 1024
        service = engine.__dict__.get("_vtt_lookup_service")
        snap = service.snapshot() if service is not None else {}
        state = engine._vtt_state_safe()
        if "error" in state:
            self.counters["state_block_invalid"] += 1
        traced = tracemalloc.get_traced_memory()[0] / 2**20 if tracemalloc.is_tracing() else None
        row = {"t": round(self.lab.now(), 1), "rss_mb": round(rss, 1), **smaps(), "malloc": mallinfo(),
               "py_traced_mb": None if traced is None else round(traced, 1), **res,
               "elements": sum(1 for _ in engine.main_pipeline.iterate_recurse()),
               "lookup_queue": snap.get("queue_depth"), "lookup_cached": snap.get("cached"),
               "vt_phase": state.get("phase"), "manual": engine.manual_mode,
               "queue_len": len(engine.log_items), "cursor": engine._queue_cursor,
               "log": getattr(engine.current_log, "hour", None), "containers": self.containers(engine)}
        emit("SAMPLE", **row)
        self.last_sample = row
        return True

    def heap_report(self, label):
        gc.collect()
        counts = Counter(type(o).__name__ for o in gc.get_objects())
        top = None
        if tracemalloc.is_tracing():
            snap = tracemalloc.take_snapshot()
            if self.heap_baseline is not None:
                diff = snap.compare_to(self.heap_baseline, "filename")[:12]
                top = [{"file": str(d.traceback[0].filename)[-70:], "size_diff_kb": round(d.size_diff / 1024, 1),
                        "count_diff": d.count_diff} for d in diff]
            else:
                self.heap_baseline = snap
        emit("HEAP", label=label, t=round(self.lab.now(), 1), objects=sum(counts.values()),
             top_types=counts.most_common(15), tracemalloc_top_growth=top)
        return True

    # -- the run ----------------------------------------------------------------------------
    def run_soak(self, seconds, *, scale=1.0, content_minutes=(22, 30), trace_heap=False):
        from library.models import LogItem, PlayEvent
        self.rng = random.Random(SEED)
        self.worker_delays = {}
        self.heap_baseline = None
        self.alive = True
        if trace_heap:
            tracemalloc.start(1)
        lab = self.lab = self.build()
        self.library = Library(self, lab, random.Random(SEED + 1))
        now = timezone.now()
        top = timezone.localtime(now).replace(minute=0, second=0, microsecond=0)
        for h in range(LOOKAHEAD_HOURS):
            start = top + timedelta(hours=h)
            minutes = self.rng.uniform(*content_minutes) if h else max(2.0, (start + timedelta(hours=1) - now)
                                                                      .total_seconds() / 60 * 0.4)
            self.library.hour_log(start, minutes * 60)
        engine = lab.engine
        self.instrument(engine)
        engine._load_log_for(timezone.localtime().date(), timezone.localtime().hour)
        self.assertTrue(engine.log_items, "the current wall-clock hour's approved log is loaded")
        poll = GLib.timeout_add(eng_module.POSITION_POLL_MS, engine._poll_position)
        self.addCleanup(lambda: GLib.MainContext.default().find_source_by_id(poll) and GLib.source_remove(poll))
        lab.play()
        with engine._vtt_starting(None):
            engine._start_next_track(slot="A")
        schedule = [(45, self.act_start_next), (80, self.act_eject), (420, self.act_manual), (540, self.act_mic),
                    (780, self.act_remote), (1200, self.act_lookup_trouble), (600, self.act_stall_next_readiness),
                    (1020, self.act_urgent), (900, self.act_swap_during_proposal), (60, self.act_rollover_in_flight),
                    (180, self.act_trim_queue),
                    (30, self.act_observe)]
        sources = [GLib.timeout_add(60000, self.guarded("sample", self.sample)),
                   GLib.timeout_add(1800000, self.guarded("heap", lambda: self.heap_report("periodic")))]
        for interval, action in schedule:
            sources.append(GLib.timeout_add(int(interval * scale * 1000), self.guarded(action.__name__, action)))
        started = time.monotonic()
        warm = False
        while time.monotonic() - started < seconds:
            pump(5.0)
            if not warm and time.monotonic() - started > min(1800, seconds / 4):
                warm = True
                self.heap_report("warm_baseline")
        self.alive = False
        for source in sources:
            if GLib.MainContext.default().find_source_by_id(source):
                GLib.source_remove(source)
        engine._set_manual_mode(False)
        engine.mic_live = False
        pump(15.0)
        self.sample()
        self.analyse_audio(flush=True)
        self.heap_report("final")
        # Terminal disposition of every claimed occurrence.
        claimed = LogItem.objects.filter(playback_claimed_at__isnull=False)
        played = set(PlayEvent.objects.values_list("log_item_id_snapshot", flat=True))
        events = Counter(PlayEvent.objects.values_list("log_item_id_snapshot", flat=True))
        on_deck = {d.log_item.id: d for d in engine.decks.values() if d is not None}
        held = engine.__dict__.get("_vtt_held_occurrence")
        dispositions = Counter()
        unexplained = []
        for row in claimed:
            if row.played_at is not None and row.id in played:
                dispositions["aired"] += 1
                continue
            removal = self.removals.get(row.id) or {}
            if row.id in on_deck:
                why = f"in_flight_at_end:{getattr(on_deck[row.id], 'admission_state', None)}"
            elif held is not None and held["log_item"].id == row.id:
                why = f"held_at_end:{held.get('reason')}"
            elif row.id in self.ejected and not removal.get("aired"):
                why = f"ejected_before_admission:{removal.get('state')}"
            elif removal.get("reason", "") and "reload" in str(removal.get("reason")):
                why = "discarded_by_reload"
            else:
                why = "UNEXPLAINED"
                unexplained.append({"log_item": row.id, "track": row.track_id, **removal,
                                    "claims": self.claims.get(row.id)})
            dispositions[why.split(":")[0]] += 1
            emit("UNAIRED", log_item=row.id, track=row.track_id, disposition=why, removal=removal,
                 claim=self.claims.get(row.id))
        duplicates = {k: v for k, v in events.items() if v > 1}
        summary = {"seconds": round(time.monotonic() - started), "counters": dict(self.counters),
                   "claimed": claimed.count(), "play_events": sum(events.values()),
                   "duplicate_play_events": duplicates, "dispositions": dict(dispositions),
                   "unexplained_unaired": unexplained, "silence_runs_with_unexplained_bins": self.silence_runs[:30],
                   "unexplained_silence_s": round(self.unexplained_bins * BIN_S, 2),
                   "harness_errors": self.harness_errors[:20], "final": self.last_sample}
        emit("SUMMARY", **summary)
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        connections.close_all()
        self.assertEqual(duplicates, {})
        self.assertEqual(unexplained, [])
        self.assertEqual(self.harness_errors, [])
        self.assertGreater(self.counters["lf_install_ok"], 0)
        self.assertEqual(self.counters["state_block_invalid"], 0)
        return summary

    def test_soak_4h(self):
        self.run_soak(4 * 3600 + 300)

    def test_soak_smoke(self):
        self.run_soak(900, scale=0.3, content_minutes=(1, 2))
