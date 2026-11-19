"""Roadmap 2.25 B7 supplemental -- portable memory workload (EVIDENCE
TOOLING; copied into each tree it measures, never part of the candidate):

    manage.py test library.tests.memsoak_vt_b7s.MemSoak.test_memsoak

The SAME continuous broadcast workload runs unchanged on r0144 and on the
candidate: real engine (transport-harness program topology), PostgreSQL
claims and PlayEvents, real VoiceTrack rows (each tree's own VT path), hour
rollover through the engine's own _roll_over_to_next_hour (which hour is
next is the only seam), Start Next, Eject, Manual/Auto and studio-mic holds.
No VT-internal hooks, so both trees are driven identically.

The harness retains NO growing history: per-minute samples are streamed to
stdout (LM-SAMPLE) and only counters are kept. Memory: VmRSS, smaps_rollup
(Pss / Private_Dirty / Anonymous), glibc mallinfo2 (arena in use vs free:
allocator high water), tracemalloc (Python-traced bytes and the files whose
allocations grew), gc object counts by type, and the sizes of every engine
container. GStreamer/native = RSS growth not explained by traced Python.
"""

from __future__ import annotations

import ctypes
import gc
import json
import random
import time
import tracemalloc
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.tests._transport_harness import start_next_command
from library.tests._vt_b0_lab import GLib, Gst, Lab, pump

SEED = 77
HOUR_ITEMS = 60
DURATION_S = 9000          # 2.5 h


def emit(kind, **row):
    print(f"LM-{kind} " + json.dumps(row, sort_keys=True, default=str), flush=True)


class _MallInfo2(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
                                               "fsmblks", "uordblks", "fordblks", "keepcost")]


def mallinfo():
    libc = ctypes.CDLL("libc.so.6")
    libc.mallinfo2.restype = _MallInfo2
    m = libc.mallinfo2()
    return {"arena_mb": round(m.arena / 2**20, 2), "in_use_mb": round(m.uordblks / 2**20, 2),
            "free_mb": round(m.fordblks / 2**20, 2), "mmap_mb": round(m.hblkhd / 2**20, 2)}


def smaps():
    out = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        key, _sep, rest = line.partition(":")
        if key in ("Rss", "Pss", "Private_Dirty", "Anonymous"):
            out[key.lower() + "_mb"] = round(int(rest.split()[0]) / 1024, 2)
    return out


def segments(total, freq):
    return [(0.15, 2200), (max(0.2, total - 0.3), freq), (0.15, 2600)]


class MemSoak(TransactionTestCase):
    def library(self, lab):
        from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Track, VoiceTrack
        rng = random.Random(SEED)
        try:
            from library.models import VoiceTrackConfig
            VoiceTrackConfig.objects.update_or_create(pk=1, defaults={"program_duck_db": -6.0, "duck_ramp_ms": 120,
                                                                     "min_gap_ms": 200})
        except Exception:
            pass
        kind = CategoryKind.objects.create(code="lmmusic", name="LM music")
        cat = Category.objects.create(code="LMMUS", name="LM music", kind=kind)
        artist = Artist.objects.create(name="LM Artist")
        tracks = []
        for n in range(36):
            length = rng.choice((7.0, 8.5, 10.0, 12.0, 14.0))
            cue = rng.choice((0.0, 0.1, 0.25))
            segs = ([(cue, 0)] if cue else []) + [(length - cue - 1.0, 600), (1.0, 1400)]
            path = lab.media(f"lm-{n}", segs, fmt="flac" if n % 3 else "wav")
            fields = {"cue_in_seconds": cue, "next_start_seconds": round(length - rng.uniform(0.6, 1.2), 3)}
            if n % 9:
                fields["intro_until_seconds"] = round(rng.uniform(2.0, 5.0), 3)
            if n % 3 == 0:
                fields["outro_starts_seconds"] = round(length - rng.uniform(2.0, 3.5), 3)
            t = Track.objects.create(filepath=str(path), filename=Path(path).name, title=f"lm-{n}", artist=artist,
                                     category=cat, ready2air=True, duration_seconds=length, **fields)
            if n % 2 == 0:
                vt = round(rng.uniform(0.8, 6.0), 3)
                VoiceTrack.objects.create(track=t, position="intro", duration_seconds=vt, gain_db=0.0,
                                          filepath=str(lab.media(f"lm-vt-i-{n}", segments(vt, 1000), fmt="flac")))
            if n % 3 == 0:
                vt = round(rng.uniform(1.0, 3.0), 3)
                VoiceTrack.objects.create(track=t, position="outro", duration_seconds=vt, gain_db=0.0,
                                          filepath=str(lab.media(f"lm-vt-o-{n}", segments(vt, 900), fmt="flac")))
            tracks.append(t)
        self.serial, self.position = 0, 0

        def hour():
            self.serial += 1
            log = PlaylistLog.objects.create(date=date(2027, 12, 1) + timedelta(days=self.serial // 24),
                                             hour=self.serial % 24, status="approved")
            rows = []
            for _n in range(HOUR_ITEMS):
                t = rng.choice(tracks)
                self.position += 1
                rows.append(LogItem.objects.create(playlist_log=log, position=self.position,
                                                   scheduled_time=timezone.now(), track=t, track_title=t.title,
                                                   track_artist=artist.name, category=cat))
            return log, list(LogItem.objects.select_related("track", "category").filter(
                pk__in=[r.pk for r in rows]).order_by("position"))
        return hour

    def run_workload(self, seconds):
        from library.models import PlayEvent
        tracemalloc.start(1)
        lab = Lab(self)
        engine = lab.engine
        for name in ("_claim_playback_occurrence", "_schedule_occurrence_air_start_from_probe",
                     "_persist_deck_duration", "_roll_over_to_next_hour", "_on_log_exhausted"):
            engine.__dict__.pop(name, None)
        engine._write_now_playing = lambda _track: None
        engine._write_rbds_category_state = lambda _track: None
        for name in ("_check_commands", "_check_stuck_decks", "_sweep_active_message_queue", "_warm_track_cache",
                     "_reload_queue_if_changed"):
            setattr(engine, name, lambda *_a, **_k: None)
        if hasattr(engine, "_vtt_lookups"):
            engine._vtt_lookup_inline = False
            self.addCleanup(lambda: engine.__dict__.get("_vtt_lookup_service") and
                            engine._vtt_lookup_service.stop(2.0))
        cfg = SimpleNamespace(ptt_auto_manual_enabled=True, enabled=False, duck_level_db=-10.0)
        engine.__dict__.setdefault("_duck_ramp_source_id", None)
        mocked = patch.object(eng_module.DuckingConfig, "load", new=lambda: cfg)
        mocked.start()
        self.addCleanup(mocked.stop)
        hour = self.library(lab)
        upcoming = [hour(), hour()]

        def peek():
            while engine.current_log is not None and upcoming[0][0].pk == engine.current_log.pk:
                upcoming.pop(0)
                upcoming.append(hour())
                counters["hours"] += 1
            return upcoming[0]

        def load(*_a, **_k):
            log, items = upcoming.pop(0)
            upcoming.append(hour())
            engine.current_log, engine.log_items, engine._queue_cursor = log, items, 0
            counters["hours"] += 1
            return True
        engine._peek_next_hour = peek
        engine._load_log_for = load
        engine._current_hour_schedule_state = lambda now, refresh=True: {
            "state": "scheduled", "has_hour_schedule": True,
            "active_key": (engine.current_log.date, engine.current_log.hour) if engine.current_log else None,
            "now_key": (now.date(), now.hour)}
        log, items = upcoming.pop(0)
        upcoming.append(hour())
        engine.current_log, engine.log_items, engine._queue_cursor = log, items, 0
        counters = Counter()
        rng = random.Random(SEED + 1)
        poll = GLib.timeout_add(eng_module.POSITION_POLL_MS, engine._poll_position)
        self.addCleanup(lambda: GLib.MainContext.default().find_source_by_id(poll) and GLib.source_remove(poll))
        lab.play()
        engine._start_next_track(slot="A")
        baseline = {}

        def trim():
            bufs = lab.h.program_buffers
            cut = 0
            limit = lab.now() - 2.0
            while cut < len(bufs) and bufs[cut][0] < limit:
                cut += 1
            del bufs[:cut]
            return True

        def act(name, fn):
            def run():
                try:
                    fn()
                    counters[f"act:{name}"] += 1
                except Exception as exc:
                    counters[f"error:{name}"] += 1
                    emit("ERROR", name=name, error=repr(exc))
                return True
            return run

        def start_next():
            if engine.__dict__.get("_start_next_active") is None and engine._live_decks():
                engine._dispatch_engine_command(start_next_command(engine, fade_ms=600))

        def eject():
            live = engine._live_decks()
            if live:
                engine._eject_deck(rng.choice(live).slot)

        def manual():
            engine._set_manual_mode(True)
            GLib.timeout_add(6000, lambda: (engine._set_manual_mode(False), False)[1])

        def mic():
            engine.mic_live = True
            engine._apply_mic_mode_hold()

            def off():
                engine.mic_live = False
                engine._apply_mic_mode_hold()
                return False
            GLib.timeout_add(4000, off)

        def containers():
            sizes = {}
            for name, value in engine.__dict__.items():
                if isinstance(value, (str, bytes)):
                    continue
                try:
                    n = len(value)
                except Exception:
                    continue
                if n >= 8:
                    sizes[name] = n
            return sizes

        def sample():
            status = Path("/proc/self/status").read_text()
            rss = int(next(x.split()[1] for x in status.splitlines() if x.startswith("VmRSS"))) / 1024
            row = {"t": round(lab.now(), 1), "rss_mb": round(rss, 2), **smaps(), "malloc": mallinfo(),
                   "py_traced_mb": round(tracemalloc.get_traced_memory()[0] / 2**20, 2), **lab.resources(),
                   "elements": sum(1 for _ in engine.main_pipeline.iterate_recurse()), "containers": containers(),
                   "counters": dict(counters)}
            emit("SAMPLE", **row)
            return True

        state = {"snap": None}

        def heap(label):
            gc.collect()
            counts = Counter(type(o).__name__ for o in gc.get_objects())
            snap = tracemalloc.take_snapshot()
            top = None
            if state["snap"] is not None:
                top = [{"file": str(d.traceback[0].filename)[-80:], "kb": round(d.size_diff / 1024, 1),
                        "count": d.count_diff} for d in snap.compare_to(state["snap"], "filename")[:15]]
            else:
                state["snap"] = snap
            emit("HEAP", label=label, t=round(lab.now(), 1), objects=sum(counts.values()),
                 top_types=counts.most_common(20), growth_since_warm=top)
            return True

        sources = [GLib.timeout_add(2000, trim), GLib.timeout_add(60000, sample),
                   GLib.timeout_add(1800000, lambda: heap("periodic")),
                   GLib.timeout_add(420000, act("start_next", start_next)), GLib.timeout_add(660000, act("eject", eject)),
                   GLib.timeout_add(540000, act("manual", manual)), GLib.timeout_add(480000, act("mic", mic))]
        started = time.monotonic()
        warm = False
        while time.monotonic() - started < seconds:
            pump(5.0)
            if not warm and time.monotonic() - started > 1800:
                warm = True
                heap("warm_baseline")
        for source in sources:
            if GLib.MainContext.default().find_source_by_id(source):
                GLib.source_remove(source)
        engine._set_manual_mode(False)
        pump(10.0)
        sample()
        heap("final")
        events = Counter(PlayEvent.objects.values_list("log_item_id_snapshot", flat=True))
        emit("SUMMARY", play_events=sum(events.values()), duplicates=sum(1 for v in events.values() if v > 1),
             counters=dict(counters), seconds=round(time.monotonic() - started))
        tracemalloc.stop()
        connections.close_all()

    def test_memsoak(self):
        self.run_workload(DURATION_S)

    def test_memsoak_smoke(self):
        self.run_workload(300)
