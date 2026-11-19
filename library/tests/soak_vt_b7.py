"""Roadmap 2.25 B7 -- extended isolated engine soak (MEASUREMENT, not a
regression test: no ``test_`` prefix; run explicitly through the runner):

    manage.py test library.tests.soak_vt_b7.VtSoak.test_soak_8h
    manage.py test library.tests.soak_vt_b7.VtSoak.test_soak_smoke

One engine (the 2.23 transport harness program topology, real GStreamer,
real time) plays a continuous, scripted broadcast day against PostgreSQL:
real LogItems, Tracks, VoiceTracks (legacy files) and VoiceTrackConfig, the
real claim / air-start / PlayEvent accounting, the real VT lookup worker
reading PostgreSQL, the production 250 ms position poll, the real Start Next,
Eject, Manual/Auto, studio-mic and Remote DJ gate authorities, and urgent
insertion at the cursor. Hour rollover runs the engine's own exhausted-log
path (``_on_log_exhausted`` -> ``_load_log_for`` -> next start) and its
look-ahead across the boundary (``_peek_next_hour``); only the wall-clock
choice of WHICH log is next is a seam (soak hours are ~10 minutes). The Hour
Refill observer is called every 30 s exactly as the observation-only runner
reads the engine (``capture_playback_window``).

Seams (documented limitations): no station I/O (engine command queue,
now-playing file, RBDS); live fill is not exercised (every soak hour has a
complete log; one deliberately short hour exercises early rollover); the
Remote DJ session is a gate stand-in (its WebRTC media path is not built).

Output: ``SOAK-SAMPLE`` lines every 60 s, ``SOAK-EVENT`` lines for every
scripted action, ``SOAK-SUMMARY`` at the end, then assertions.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.services import hour_refill_projection, vt_transition
from library.tests._transport_harness import start_next_command
from library.tests._vt_b0_lab import NS, GLib, Gst, Lab, pump, wait_until
from library.tests._vt_integration_lab import vt_segments

RNG_SEED = 2025
HOUR_ITEMS = 60
SILENCE_RMS = 0.003
SILENCE_REPORT_S = 2.0
WARMUP_S = 300.0
SAMPLE_S = 60.0


class SoakLibrary:
    """Tracks, VoiceTracks and soak hours (PlaylistLogs) in PostgreSQL."""

    def __init__(self, case, lab):
        from library.models import Artist, Category, CategoryKind, VoiceTrackConfig
        self.case, self.lab = case, lab
        self.rng = random.Random(RNG_SEED)
        VoiceTrackConfig.objects.update_or_create(pk=1, defaults={"program_duck_db": -6.0, "duck_ramp_ms": 120,
                                                                 "min_gap_ms": 200})
        music_kind = CategoryKind.objects.create(code="soakmusic", name="Soak music")
        id_kind = CategoryKind.objects.create(code="soakid", name="Soak IDs")
        self.cats = {
            "music": Category.objects.create(code="SOAKMUS", name="Soak music", kind=music_kind),
            "id": Category.objects.create(code="SOAKID", name="Soak IDs", kind=id_kind),
            "promo": Category.objects.create(code="SOAKPRO", name="Soak promos", kind=id_kind),
        }
        self.wx = Category.objects.filter(code="WxAlert").first()
        self.artist = Artist.objects.create(name="Soak Artist")
        self.tracks = {"music": [], "id": [], "promo": [], "urgent": []}
        self.hour_serial = 0
        self.position = 0
        self.hour_items = case.hour_items
        self.build_pool()

    def track(self, name, segments, cat, **fields):
        from library.models import Track
        fmt = "flac" if self.rng.random() < 0.4 else "wav"
        path = self.lab.media(name, segments, fmt=fmt)
        duration = round(sum(s for s, _f in segments), 6)
        return Track.objects.create(filepath=str(path), filename=Path(path).name, title=name, artist=self.artist,
                                    category=cat, ready2air=True, duration_seconds=duration, **fields)

    def voicetrack(self, track, position, seconds, *, corrupt=False):
        from library.models import VoiceTrack
        if corrupt:
            path = self.lab.root / f"soak-corrupt-{track.pk}-{position}.flac"
            path.write_bytes(b"\x00\xffnot audio" * 2048)
        else:
            path = self.lab.media(f"soak-vt-{track.pk}-{position}-{seconds}",
                                  vt_segments(seconds, 1000 if position == "intro" else 900))
        return VoiceTrack.objects.create(track=track, position=position, filepath=str(path),
                                         duration_seconds=seconds, gain_db=0.0)

    def build_pool(self):
        rng = self.rng
        for n in range(36):
            length = rng.choice((7.0, 8.5, 10.0, 12.0, 14.0))
            cue = rng.choice((0.0, 0.1, 0.25))
            intro_until = None if n % 9 == 0 else round(rng.uniform(2.0, 5.0), 3)
            freq = 600
            segs = ([(cue, 0)] if cue else []) + [(length - cue - 1.0, freq), (1.0, 1400)]
            fields = {"cue_in_seconds": cue, "next_start_seconds": round(length - rng.uniform(0.6, 1.2), 3)}
            if intro_until:
                fields["intro_until_seconds"] = intro_until
            if n % 3 == 0:
                fields["outro_starts_seconds"] = round(length - rng.uniform(2.0, 3.5), 3)
            t = self.track(f"soak-music-{n}", segs, self.cats["music"], **fields)
            if n % 2 == 0:
                self.voicetrack(t, "intro", round(rng.uniform(0.8, 6.0), 3), corrupt=(n % 14 == 0))
            if n % 3 == 0:
                self.voicetrack(t, "outro", round(rng.uniform(1.0, 3.0), 3), corrupt=(n % 15 == 0))
            self.tracks["music"].append(t)
        for n in range(6):
            self.tracks["id"].append(self.track(f"soak-id-{n}", [(rng.choice((2.0, 2.5, 3.0)), 700)],
                                                self.cats["id"], next_start_seconds=None))
        for n in range(6):
            self.tracks["promo"].append(self.track(f"soak-promo-{n}", [(rng.choice((4.0, 5.0, 6.0)), 800)],
                                                   self.cats["promo"]))
        # An urgent item for the VT urgent rule (marked urgent through the
        # engine's own _vtt_mark_urgent). A real WxAlert occurrence opens only
        # for a published coordinator generation (r0137, its own suites).
        self.tracks["urgent"].append(self.track("soak-urgent", [(3.0, 300)], self.cats["promo"]))
        # Invalid media: a music Track whose file disappears (skipped as unplayable).
        missing = self.track("soak-missing", [(6.0, 600)], self.cats["music"])
        Path(missing.filepath).unlink()
        self.tracks["missing"] = [missing]

    def next_hour(self, *, short=False):
        from library.models import LogItem, PlaylistLog
        self.hour_serial += 1
        log = PlaylistLog.objects.create(date=date(2027, 12, 1) + timedelta(days=self.hour_serial // 24),
                                         hour=self.hour_serial % 24, status="approved")
        items = []
        count = 12 if short else self.hour_items
        for n in range(count):
            roll = self.rng.random()
            if n % 17 == 5:
                kinds = ("id", "id")            # back-to-back short items
            elif n % 29 == 11:
                kinds = ("missing",)
            elif roll < 0.12:
                kinds = ("id",)
            elif roll < 0.24:
                kinds = ("promo",)
            else:
                kinds = ("music",)
            for kind in kinds:
                track = self.rng.choice(self.tracks[kind])
                self.position += 1
                row = LogItem.objects.create(
                    playlist_log=log, position=self.position, scheduled_time=timezone.now(), track=track,
                    track_title=track.title, track_artist=self.artist.name, category=track.category)
                items.append(row)
        rows = list(LogItem.objects.select_related("track", "category").filter(
            pk__in=[i.pk for i in items]).order_by("position"))
        return log, rows

    def urgent_item(self, log):
        from library.models import LogItem
        if not self.tracks["urgent"]:
            return None
        track = self.tracks["urgent"][0]
        self.position += 1
        row = LogItem.objects.create(playlist_log=log, position=self.position, scheduled_time=timezone.now(),
                                     track=track, track_title=track.title, track_artist=self.artist.name,
                                     category=track.category)
        return LogItem.objects.select_related("track", "category").get(pk=row.pk)


class VtSoak(TransactionTestCase):
    def setUp(self):
        self.events = []
        self.samples = []
        self.counters = Counter()
        self.vt_outcomes = Counter()
        self.reasons = Counter()
        self.harness_errors = []
        self.expected_quiet = []        # (start_running, end_running): intended holds
        self.silences = []
        self.recoveries = []

    # -- engine --------------------------------------------------------------------
    def build(self):
        lab = Lab(self)
        engine = lab.engine
        for name in ("_claim_playback_occurrence", "_schedule_occurrence_air_start_from_probe",
                     "_persist_deck_duration"):
            engine.__dict__.pop(name, None)
        engine._write_now_playing = lambda _track: None
        engine._write_rbds_category_state = lambda _track: None
        for name in ("_check_commands", "_check_stuck_decks", "_sweep_active_message_queue", "_warm_track_cache",
                     "_reload_queue_if_changed"):
            setattr(engine, name, lambda *_a, **_k: None)
        engine._vtt_lookup_inline = False
        self.addCleanup(lambda: engine.__dict__.get("_vtt_lookup_service") and
                        engine._vtt_lookup_service.stop(2.0))
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

    def wire_hours(self, lab, library):
        engine = lab.engine
        hours = [library.next_hour()]
        upcoming = [library.next_hour()]
        state = {"loaded": 1, "short_every": 5}

        def advance(how):
            log, items = upcoming.pop(0)
            state["loaded"] += 1
            short = state["loaded"] % state["short_every"] == 0
            upcoming.append(library.next_hour(short=short))
            self.event("hour_rollover", log_id=log.pk, items=len(items), path=how)
            return log, items

        def load(*_args, **_kwargs):
            # _on_log_exhausted's path (natural EOS with nothing rolled over).
            log, items = advance("on_log_exhausted")
            engine.current_log = log
            engine.log_items = items
            engine._queue_cursor = 0
            return True

        def peek():
            # The REAL _roll_over_to_next_hour installs what this returns; once
            # it is the active log, the next soak hour is what follows it.
            while engine.current_log is not None and upcoming[0][0].pk == engine.current_log.pk:
                advance("roll_over_to_next_hour")
            return upcoming[0]
        # The transport harness stubs the rollover path; the soak runs the real one.
        for name in ("_roll_over_to_next_hour", "_on_log_exhausted"):
            engine.__dict__.pop(name, None)
        engine._load_log_for = load
        engine._peek_next_hour = peek
        engine._current_hour_schedule_state = lambda now, refresh=True: {
            "state": "scheduled", "has_hour_schedule": True,
            "active_key": (engine.current_log.date, engine.current_log.hour) if engine.current_log else None,
            "now_key": (now.date(), now.hour)}
        log, items = hours[0]
        engine.current_log = log
        engine.log_items = items
        engine._queue_cursor = 0

    # -- bookkeeping --------------------------------------------------------------
    def event(self, kind, **detail):
        now = self.lab.now() if getattr(self, "lab", None) else 0.0
        self.counters[f"event:{kind}"] += 1
        row = {"t": round(now, 3), "kind": kind, **detail}
        self.events.append(row)
        print("SOAK-EVENT " + json.dumps(row, sort_keys=True, default=str), flush=True)

    def guarded(self, name, fn):
        def run():
            try:
                fn()
            except Exception as exc:          # a harness failure is reported, never hides the soak
                self.harness_errors.append(f"{name}: {exc!r}")
                print(f"SOAK-HARNESS-ERROR {name}: {exc!r}", flush=True)
            return False
        return run

    def instrument(self, engine):
        finish = engine._vtt_finish

        def counted(record, reason):
            if not record.done:
                wall = lambda ns: None   # noqa: E731
                for side in (vt_transition.OUTRO, vt_transition.INTRO):
                    status = engine._vtt_side_state(record, side, wall)["status"]
                    self.vt_outcomes[f"{side}:{status}"] += 1
                    why = record.outro_reason if side == vt_transition.OUTRO else record.intro_reason
                    if why:
                        self.reasons[f"{side}:{why}"] += 1
                self.vt_outcomes[f"finish:{str(reason).split(':')[0]}"] += 1
                for _t, name, detail in record.events:
                    if name.startswith("fallback") or name in ("readmit", "late_intro_upgrade"):
                        self.counters[f"record:{name}"] += 1
            return finish(record, reason)
        engine._vtt_finish = counted
        report = engine._vtt_report_abnormal

        def abnormal(kind, record, log_item_id, **detail):
            self.counters[f"abnormal:{kind}"] += 1
            return report(kind, record, log_item_id, **detail)
        engine._vtt_report_abnormal = abnormal

    # -- scripted operator / environment actions ---------------------------------
    def quiet(self, seconds):
        start = self.lab.now()
        self.expected_quiet.append((start, start + seconds + 3.0))

    def act_start_next(self):
        engine = self.lab.engine
        if engine.__dict__.get("_start_next_active") is None:
            engine._dispatch_engine_command(start_next_command(engine, fade_ms=600))
            self.event("start_next")

    def act_eject(self):
        engine = self.lab.engine
        live = engine._live_decks()
        if live:
            deck = self.rng.choice(live)
            engine._eject_deck(deck.slot)
            self.event("eject", slot=deck.slot, log_item=deck.log_item.id)

    def act_manual(self):
        engine = self.lab.engine
        engine._set_manual_mode(True)
        self.quiet(6.0)
        self.event("manual_on")
        GLib.timeout_add(6000, self.guarded("manual_off", lambda: (engine._set_manual_mode(False),
                                                                    self.event("manual_off"))))

    def act_mic(self):
        engine = self.lab.engine
        engine.mic_live = True
        engine._apply_mic_mode_hold()
        self.quiet(4.0)
        self.event("mic_on")

        def off():
            engine.mic_live = False
            engine._apply_mic_mode_hold()
            self.event("mic_off")
        GLib.timeout_add(4000, self.guarded("mic_off", off))

    def act_remote(self):
        engine = self.lab.engine
        engine._remote_dj_set_gate(True)
        self.quiet(3.0)
        self.event("remote_gate_on")

        def reconnect():
            engine._remote_dj_set_gate(False)
            engine.remote_dj_session = None            # session lost ...
            engine._apply_mic_mode_hold()
            engine.remote_dj_session = self.dj         # ... and reconnected with the gate Off
            engine._apply_mic_mode_hold()
            self.event("remote_reconnect")
        GLib.timeout_add(3000, self.guarded("remote_reconnect", reconnect))

    def act_lookup_trouble(self):
        engine = self.lab.engine
        service = engine._vtt_lookups()
        resolve = service._resolve
        mode = {"phase": "slow"}

        def troubled(key, payload):
            if mode["phase"] == "slow":
                time.sleep(1.5)
            elif mode["phase"] == "fail":
                raise RuntimeError("soak: database unavailable")
            return resolve(key, payload)
        service._resolve = troubled
        self.event("lookup_slow")

        def fail():
            mode["phase"] = "fail"
            self.event("lookup_fail")

        def heal():
            mode["phase"] = "ok"
            service._resolve = resolve
            self.event("lookup_healed")
        GLib.timeout_add(30000, self.guarded("lookup_fail", fail))
        GLib.timeout_add(50000, self.guarded("lookup_heal", heal))

    def act_urgent(self):
        engine = self.lab.engine
        item = self.library.urgent_item(engine.current_log)
        if item is None:
            return
        engine.log_items.insert(engine._queue_cursor, item)
        engine._vtt_mark_urgent(item)
        engine._vtt_queue_changed("urgent_insert")
        self.event("urgent", log_item=item.id)

    def act_observe_hour_refill(self):
        engine = self.lab.engine
        window = hour_refill_projection.capture_playback_window(engine)
        phase = getattr(window, "vt_phase", None)
        self.counters[f"hr_phase:{phase}"] += 1

    # -- measurements ------------------------------------------------------------------
    def analyse_audio(self):
        """Silence runs in the recorded program since the last call; the
        analysed buffers are dropped (bounded memory over hours)."""
        bufs = self.lab.h.program_buffers
        keep_after = self.lab.now() - 3.0
        cut = 0
        while cut < len(bufs) and bufs[cut][0] < keep_after:
            cut += 1
        chunk = bufs[:cut]
        del bufs[:cut]
        window = self.lab.rate // 100
        for running, samples in chunk:
            for i in range(0, len(samples) - window + 1, window):
                seg = samples[i:i + window]
                rms = (sum(v * v for v in seg) / len(seg)) ** 0.5
                t = running + i / self.lab.rate
                if rms < SILENCE_RMS:
                    if self.silence_start is None:
                        self.silence_start = t
                elif self.silence_start is not None:
                    length = t - self.silence_start
                    if length >= SILENCE_REPORT_S:
                        explained = any(a <= self.silence_start <= b for a, b in self.expected_quiet)
                        self.silences.append((round(self.silence_start, 2), round(length, 2), explained))
                        print(f"SOAK-SILENCE start={self.silence_start:.2f} seconds={length:.2f} "
                              f"explained={explained}", flush=True)
                    self.silence_start = None

    def sample(self):
        engine = self.lab.engine
        self.analyse_audio()
        if self.silence_start is not None and self.lab.now() - self.silence_start >= 30.0:
            # A silence that has not ended is reported while it lasts (a stall).
            explained = any(a <= self.silence_start <= b for a, b in self.expected_quiet)
            key = round(self.silence_start, 2)
            if not any(s0 == key for s0, _l, _e in self.silences):
                self.silences.append((key, round(self.lab.now() - self.silence_start, 2), explained))
                print(f"SOAK-SILENCE start={key:.2f} ongoing={self.lab.now() - self.silence_start:.2f} "
                      f"explained={explained}", flush=True)
        res = self.lab.resources()
        status = Path("/proc/self/status").read_text()
        rss_kb = int(next(line.split()[1] for line in status.splitlines() if line.startswith("VmRSS")))
        service = engine.__dict__.get("_vtt_lookup_service")
        lookups = service.snapshot() if service is not None else {}
        teardown = engine.__dict__.get("_vt2_teardown")
        tsnap = teardown.snapshot() if teardown is not None else {}
        live_ids = [d.log_item.id for d in engine.decks.values() if d is not None and not d.retirement_started]
        if len(live_ids) != len(set(live_ids)):
            self.counters["duplicate_live_occurrence"] += 1
        state = engine._vtt_state_safe()
        text = json.dumps(state)                    # engine_state.json "vt_transition", every minute
        if "error" in state or str(self.lab.root) in text:
            self.counters["state_block_invalid"] += 1
        if not state.get("active") and state.get("current") is not None:
            self.counters["state_terminal_active"] += 1
        row = {
            "t": round(self.lab.now(), 1), "rss_mb": round(rss_kb / 1024, 1), **res,
            "vt_phase": state.get("phase"),
            "fires_registry": len(engine.__dict__.get("_vt2_fires", {})),
            "linked_decks": sum(1 for d in engine.decks.values() if d is not None and d.mixer_pad is not None),
            "elements": sum(1 for _ in engine.main_pipeline.iterate_recurse()),
            "lookup_queue": lookups.get("queue_depth"), "lookup_cached": lookups.get("cached"),
            "lookup_inflight": lookups.get("inflight"), "lookup_restarts": lookups.get("restarts"),
            "lookup_unavailable": lookups.get("unavailable"), "lookup_saturated": lookups.get("saturated"),
            "teardown_poisoned": tsnap.get("poisoned"), "teardown_completed": tsnap.get("completed"),
            "vt_active": bool(engine._vtt_records()), "held": engine.__dict__.get("_vtt_held_occurrence") is not None,
            "manual": engine.manual_mode,
        }
        self.samples.append(row)
        print("SOAK-SAMPLE " + json.dumps(row, sort_keys=True), flush=True)
        return True

    # -- the run ------------------------------------------------------------------------
    def run_soak(self, seconds, schedule, *, hour_items=HOUR_ITEMS):
        self.hour_items = hour_items
        from library.models import LogItem, PlayEvent
        self.rng = random.Random(RNG_SEED + 1)
        self.silence_start = None
        lab = self.lab = self.build()
        self.library = SoakLibrary(self, lab)
        self.wire_hours(lab, self.library)
        engine = lab.engine
        self.instrument(engine)
        poll = GLib.timeout_add(eng_module.POSITION_POLL_MS, engine._poll_position)
        self.addCleanup(lambda: GLib.MainContext.default().find_source_by_id(poll) and GLib.source_remove(poll))
        lab.play()
        with engine._vtt_starting(None):
            engine._start_next_track(slot="A")
        started = time.monotonic()
        sources = [GLib.timeout_add(int(SAMPLE_S * 1000), lambda: (self.guarded("sample", self.sample)(), True)[1]),
                   GLib.timeout_add(30000, lambda: (self.guarded("hr", self.act_observe_hour_refill)(), True)[1])]
        for interval, action in schedule:
            name = action.__name__
            sources.append(GLib.timeout_add(int(interval * 1000),
                                            lambda a=action, n=name: (self.guarded(n, a)(), True)[1]))
        baseline = None
        while time.monotonic() - started < seconds:
            pump(5.0)
            if baseline is None and time.monotonic() - started >= WARMUP_S and self.samples:
                baseline = dict(self.samples[-1])
                print("SOAK-BASELINE " + json.dumps(baseline, sort_keys=True), flush=True)
        for source in sources:
            if GLib.MainContext.default().find_source_by_id(source):
                GLib.source_remove(source)
        engine._set_manual_mode(False)
        pump(20.0)
        self.sample()
        final = self.samples[-1]
        baseline = baseline or self.samples[0]
        events_per_item = Counter(PlayEvent.objects.values_list("log_item_id_snapshot", flat=True))
        duplicates = {k: v for k, v in events_per_item.items() if v > 1}
        claimed = LogItem.objects.filter(playback_claimed_at__isnull=False).count()
        played = LogItem.objects.filter(played_at__isnull=False).count()
        hours = self.counters["event:hour_rollover"]
        rss_rate = (final["rss_mb"] - baseline["rss_mb"]) / max(1e-6, (final["t"] - baseline["t"]) / 3600.0)
        unexplained = [s for s in self.silences if not s[2]]
        summary = {
            "seconds": round(time.monotonic() - started), "hours_rolled": hours,
            "play_events": sum(events_per_item.values()), "occurrences_played": played,
            "occurrences_claimed": claimed, "duplicate_play_events": len(duplicates),
            "duplicate_live_occurrence": self.counters["duplicate_live_occurrence"],
            "vt_outcomes": dict(self.vt_outcomes), "suppression_reasons": dict(self.reasons.most_common(20)),
            "silences_total": len(self.silences), "silences_unexplained": unexplained[:20],
            "rss_baseline_mb": baseline["rss_mb"], "rss_final_mb": final["rss_mb"],
            "rss_growth_mb_per_hour": round(rss_rate, 2),
            "baseline": {k: baseline.get(k) for k in ("deck_pads", "fx_pads", "children", "elements", "threads", "fds")},
            "final": {k: final.get(k) for k in ("deck_pads", "fx_pads", "children", "elements", "threads", "fds")},
            "max_elements": max((s.get("elements") or 0) for s in self.samples),
            "max_linked_decks": max((s.get("linked_decks") or 0) for s in self.samples),
            "rss_by_hour_mb": [s["rss_mb"] for s in self.samples[::60]],
            "max_lookup_queue": max((s.get("lookup_queue") or 0) for s in self.samples),
            "max_lookup_cached": max((s.get("lookup_cached") or 0) for s in self.samples),
            "teardown_poisoned_ever": any(s.get("teardown_poisoned") for s in self.samples),
            "counters": {k: v for k, v in self.counters.items()}, "harness_errors": self.harness_errors[:20],
        }
        print("SOAK-SUMMARY " + json.dumps(summary, sort_keys=True, default=str), flush=True)
        connections.close_all()
        self.assertEqual(summary["duplicate_play_events"], 0, duplicates)
        self.assertEqual(summary["duplicate_live_occurrence"], 0)
        self.assertEqual(unexplained, [], "unexplained program silence")
        self.assertEqual(self.harness_errors, [])
        self.assertFalse(summary["teardown_poisoned_ever"])
        for key in ("deck_pads", "fx_pads", "children"):
            self.assertLessEqual(final[key], baseline[key] + 2, key)
        self.assertLessEqual(final["threads"] - baseline["threads"], 4)
        self.assertLessEqual(final["fds"] - baseline["fds"], 16)
        self.assertGreater(hours, 0)
        self.assertEqual(self.counters["state_block_invalid"], 0)
        self.assertEqual(self.counters["state_terminal_active"], 0)
        return summary

    def schedule(self, scale=1.0):
        return [(420 * scale, self.act_start_next), (660 * scale, self.act_eject), (540 * scale, self.act_manual),
                (480 * scale, self.act_mic), (780 * scale, self.act_remote), (900 * scale, self.act_lookup_trouble),
                (1020 * scale, self.act_urgent)]

    def test_soak_8h(self):
        self.run_soak(8 * 3600 + 600, self.schedule())

    def test_soak_smoke(self):
        global WARMUP_S
        WARMUP_S = 60.0
        self.run_soak(900, self.schedule(scale=0.12), hour_items=20)

    def test_soak_rollover_probe(self):
        """Diagnostic: tiny hours, no scripted events -- hour rollover only."""
        global WARMUP_S
        WARMUP_S = 30.0
        engine_probe = []

        def probe():
            engine = self.lab.engine
            item = engine._start_next_peek_next_item()
            coords = {slot: (engine._deck_slot_available(slot),
                             engine._ensure_deck_teardown_coordinator(slot).snapshot().get("active_generation"))
                      for slot in ("A", "B")}
            if not any(engine.decks.values()) and engine._queue_cursor >= len(engine.log_items):
                import traceback
                try:
                    with engine._vtt_starting(None):
                        engine._start_next_track(slot="A")
                    print(f"SOAK-PROBE start_next_track returned; decks={[d for d in engine.decks.values() if d]}")
                except Exception:
                    print("SOAK-PROBE start_next_track raised:\n" + traceback.format_exc(), flush=True)
            print(f"SOAK-PROBE slots={coords} triggered={engine._next_triggered} owns={engine._vtt_owns_next_start()} "
                  f"cursor={engine._queue_cursor}/{len(engine.log_items)} "
                  f"peek={getattr(item, 'id', None)} decks={[getattr(d.log_item, 'id', None) for d in engine.decks.values() if d]}",
                  flush=True)
        self.run_soak(240, [(10, probe)], hour_items=5)
