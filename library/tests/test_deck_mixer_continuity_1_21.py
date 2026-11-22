"""Roadmap 1.21 Phase A: the deck mixer's timeline survives a zero-deck idle.

A non-live deck audiomixer stops advancing when its last deck pad is released while master_mixer and the pipeline
clock continue. The first deck after the idle then waits behind the mixer regenerating the idle span as silence,
drained at ~5.5x real time: dead air of about idle/4.5, with the occurrence already claimed and its PlayEvent
written while nothing of it is audible. The deck mixer is now constructed live (``make_deck_mixer``,
``force-live``), so it keeps aggregating silence on the clock through the idle.

These tests measure the mechanism, not the property: the deck mixer's own output running time during a real
zero-deck idle (src-pad probe), its progression rate after the next start (a catch-up shows as >> 1x real time),
the item's tone in the program PCM, and the accounting (one PlayEvent, committed only after the deck's
first-real-audio boundary). The engine runs its own exhausted-queue retry for the idle and starts the next item
itself. Thresholds come from healthy measurements (1.21 A evidence, 44.1 and 48 kHz, 15-240 s idles): idle lag
<= 0.022 s, progression <= 1.018x, first audio 0.262-0.295 s after the deck exists (a hot-pipeline start: 0.32 s),
tone present >= 5.21 of 5.5 s. With the r0145 mixer the same 30 s idle leaves the mixer ~30 s behind, catching up
at ~5.6x with the tone absent for the whole window.

Phase A2 adds: the deck bus is fixed to S16LE from startup (``make_deck_bus_caps``) -- every CAPS event on the deck
mixer's src must be that one format (the unfixed live mixer negotiated S32LE with no pad and renegotiated to S16LE
on the first deck, which corrupted deck audio); the item's program PCM is checked sample-wise against a fitted sine
(no holes, steps or corruption -- not only a spectral classifier); and the PlayEvent commit is correlated with the
SAME item's own tone reaching the program within a bounded window (each generation has its own frequency).
Measured healthy (A2 evidence): commit 0.262-0.276 s before program onset; residual <= 0.003 % of the amplitude.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connections
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.tests._vt_b0_lab import GLib, Gst, Lab, pump

FIRST_TONE, NEXT_TONE = 900, 600
IDLE_S = 30.0
# Healthy measured maxima, with margin, far below the defect's values.
MAX_IDLE_LAG_S = 0.25          # healthy <= 0.022; defect: the whole idle (~30 s here)
MIN_IDLE_BUFFERS_PER_S = 90    # healthy 100/s (10 ms output buffers); defect: 0
MAX_PROGRESSION_RATE = 1.25    # healthy <= 1.018x real time; defect: 5.6-5.9x
MAX_FIRST_AUDIO_S = 0.6        # healthy 0.262-0.295 s after the deck exists; defect: never within 6 s
ONSET_MARGIN_OVER_HOT_START_S = 0.1
MIN_TONE_PRESENT_S = 5.0       # of the 5.5 s after the deck exists; healthy >= 5.21; defect: 0.0
MAX_COMMIT_TO_PROGRAM_S = 0.5  # PlayEvent commit -> the item's own tone in the program; healthy 0.262-0.276 s
MAX_RESIDUAL_PCT = 2.0         # per 10 ms frame, residual RMS vs a fitted sine; healthy ~0.003 % (16-bit quantisation)
MAX_AMPLITUDE_CV_PCT = 1.0     # frame-to-frame amplitude variation over 1 s; healthy 0.0 %


def _sine_fit(samples, rate, freq):
    """Least-squares DC + sine + cosine at ``freq``: (amplitude, residual RMS)."""
    import math
    n = len(samples)
    w = 2 * math.pi * freq / rate
    c = [math.cos(w * i) for i in range(n)]
    s = [math.sin(w * i) for i in range(n)]
    m = sum(samples) / n
    a = 2 * sum((samples[i] - m) * c[i] for i in range(n)) / n
    b = 2 * sum((samples[i] - m) * s[i] for i in range(n)) / n
    resid = math.sqrt(sum((samples[i] - m - a * c[i] - b * s[i]) ** 2 for i in range(n)) / n)
    return math.hypot(a, b), resid


def pcm_integrity(trace, freq, start_s, seconds=1.0):
    """Raw program samples of one tone, 10 ms frames: (max residual %, amplitude CV %, frames)."""
    import math
    rate = trace.rate
    frame = rate // 100
    i0 = trace.index_of(start_s)
    amps, worst = [], 0.0
    for k in range(int(seconds * 100)):
        seg = trace.x[i0 + k * frame:i0 + (k + 1) * frame]
        if len(seg) < frame:
            break
        amp, resid = _sine_fit(seg, rate, freq)
        amps.append(amp)
        worst = max(worst, 100.0 * resid / amp if amp > 0 else 100.0)
    mean = sum(amps) / len(amps) if amps else 0.0
    cv = 100.0 * math.sqrt(sum((v - mean) ** 2 for v in amps) / len(amps)) / mean if mean else 100.0
    return worst, cv, len(amps)


class DeckMixerConstructionTests(SimpleTestCase):
    def test_the_deck_mixer_is_constructed_live(self):
        mixer, force_live = eng_module.make_deck_mixer("mixer")
        self.assertTrue(force_live)
        self.assertEqual(mixer.get_factory().get_name(), "audiomixer")
        self.assertEqual(mixer.get_name(), "mixer")
        self.assertIs(mixer.get_property("force-live"), True)
        for rate in (44100, 48000):
            caps = eng_module.make_deck_bus_caps(rate).get_property("caps").get_structure(0)
            self.assertEqual((caps.get_value("format"), caps.get_value("rate"), caps.get_value("channels"),
                              caps.get_value("layout")), ("S16LE", rate, 2, "interleaved"))
        # Nothing else about the aggregator changes (latency changes once truncated outgoing tails).
        plain = Gst.ElementFactory.make("audiomixer", None)
        for name in ("latency", "min-upstream-latency", "start-time-selection", "output-buffer-duration",
                     "ignore-inactive-pads"):
            self.assertEqual(mixer.get_property(name), plain.get_property(name), name)

    def test_force_live_is_construct_only(self):
        """Why the helper constructs it: setting it afterwards is refused and leaves the mixer non-live."""
        mixer = Gst.ElementFactory.make("audiomixer", None)
        with self.assertRaises(TypeError):
            mixer.set_property("force-live", True)
        self.assertIs(mixer.get_property("force-live"), False)


class _ZeroDeckIdle(TransactionTestCase):
    rate = 44100

    def _probe(self, element, key):
        tel = self.tel

        def on_buffer(pad, info):
            buf = info.get_buffer()
            if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
                return Gst.PadProbeReturn.OK
            end = buf.pts + (buf.duration if buf.duration != Gst.CLOCK_TIME_NONE else 0)
            event = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
            if event is not None:
                running = event.parse_segment().to_running_time(Gst.Format.TIME, end)
                if running != Gst.CLOCK_TIME_NONE:
                    end = running
            tel[key] = end
            tel[key + "_buffers"] = tel.get(key + "_buffers", 0) + 1
            return Gst.PadProbeReturn.OK
        element.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer)

    def _sample(self):
        engine = self.lab.engine
        running = engine.main_pipeline.get_clock().get_time() - engine.main_pipeline.get_base_time()
        end = self.tel.get("deck_mixer")
        return {"running_s": running / 1e9, "deck_end_s": None if end is None else end / 1e9,
                "deck_lag_s": None if end is None else (running - end) / 1e9,
                "deck_buffers": self.tel.get("deck_mixer_buffers", 0), "deck_pads": len(engine.mixer.sinkpads)}

    def test_first_start_after_a_zero_deck_idle_airs_promptly_once(self):
        from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, PlayEvent, Track
        lab = self.lab = Lab(self, rate=self.rate)
        engine = lab.engine
        self.tel = {}
        self._probe(engine.mixer, "deck_mixer")
        # Every CAPS event on the deck mixer's src from the start of the pipeline (Phase A2: exactly one format).
        caps_seen = []

        def on_event(_pad, info):
            event = info.get_event()
            if event.type == Gst.EventType.CAPS:
                st = event.parse_caps().get_structure(0)
                caps_seen.append((st.get_value("format"), st.get_value("rate"), st.get_value("channels"),
                                  st.get_value("layout")))
            return Gst.PadProbeReturn.OK
        engine.mixer.get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_event)
        for name in ("_claim_playback_occurrence", "_schedule_occurrence_air_start_from_probe",
                     "_persist_deck_duration", "_roll_over_to_next_hour", "_on_log_exhausted"):
            engine.__dict__.pop(name, None)
        engine._write_now_playing = lambda _track: None
        engine._write_rbds_category_state = lambda _track: None
        for name in ("_check_commands", "_check_stuck_decks", "_sweep_active_message_queue", "_warm_track_cache",
                     "_reload_queue_if_changed"):
            setattr(engine, name, lambda *_a, **_k: None)
        engine._vtt_lookup_inline = False
        self.addCleanup(lambda: engine.__dict__.get("_vtt_lookup_service") and engine._vtt_lookup_service.stop(2.0))
        cfg = SimpleNamespace(ptt_auto_manual_enabled=True, enabled=False, duck_level_db=-10.0)
        engine.__dict__.setdefault("_duck_ramp_source_id", None)
        mocked = patch.object(eng_module.DuckingConfig, "load", new=lambda: cfg)
        mocked.start()
        self.addCleanup(mocked.stop)

        kind = CategoryKind.objects.create(code="dmmusic", name="DM music")
        cat = Category.objects.create(code="DMMUS", name="DM music", kind=kind)
        artist = Artist.objects.create(name="DM Artist")

        def track(name, freq, length=8.0):
            path = lab.media(name, [(length - 1.0, freq), (1.0, 1400)], fmt="flac")
            return Track.objects.create(filepath=str(path), filename=Path(path).name, title=name, artist=artist,
                                        category=cat, ready2air=True, duration_seconds=length,
                                        next_start_seconds=length - 0.8)
        position = [0]

        def hour(h, tracks):
            log = PlaylistLog.objects.create(date=date(2027, 11, 1), hour=h, status="approved")
            rows = []
            for t in tracks:
                position[0] += 1
                rows.append(LogItem.objects.create(playlist_log=log, position=position[0],
                                                   scheduled_time=timezone.now(), track=t, track_title=t.title,
                                                   track_artist=artist.name, category=cat))
            return log, list(LogItem.objects.select_related("track", "category").filter(
                pk__in=[r.pk for r in rows]).order_by("position"))

        log1, items1 = hour(1, [track(f"dm-first-{n}", FIRST_TONE) for n in range(2)])
        log2, items2 = hour(2, [track("dm-next", NEXT_TONE)])
        available = {"next": False}
        engine._peek_next_hour = lambda: (log2, items2) if available["next"] else None
        engine._load_log_for = lambda *_a, **_k: True
        engine._current_hour_schedule_state = lambda now, refresh=True: {
            "state": "scheduled", "has_hour_schedule": True,
            "active_key": (engine.current_log.date, engine.current_log.hour) if engine.current_log else None,
            "now_key": (engine.current_log.date, engine.current_log.hour) if engine.current_log else None}
        engine._try_extend_live_log_async = lambda *a, **k: False      # the live-fill dispatch stays in flight
        engine.current_log, engine.log_items, engine._queue_cursor = log1, items1, 0

        # First-real-audio boundary (deck streaming thread) and PlayEvent commit (GLib), in running seconds.
        air = {}
        boundary, commit = engine._schedule_occurrence_air_start_from_probe, engine._record_occurrence_air_start

        def on_boundary(deck, air_started_at=None):
            air.setdefault(deck.log_item.id, {}).setdefault("boundary", lab.now())
            return boundary(deck, air_started_at)

        def on_commit(slot, generation, log_item_id, *args, **kwargs):
            result = commit(slot, generation, log_item_id, *args, **kwargs)
            air.setdefault(log_item_id, {}).setdefault("commit", lab.now())
            return result
        engine._schedule_occurrence_air_start_from_probe = on_boundary
        engine._record_occurrence_air_start = on_commit

        poll = GLib.timeout_add(eng_module.POSITION_POLL_MS, engine._poll_position)
        self.addCleanup(lambda: GLib.MainContext.default().find_source_by_id(poll) and GLib.source_remove(poll))
        lab.play()
        hot_created = lab.now()
        engine._start_next_track(slot="A")

        def no_deck():
            return all(d is None for d in engine.decks.values())
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline and not (engine._queue_cursor >= len(engine.log_items) and no_deck()):
            pump(0.2)
        self.assertTrue(no_deck(), "the first hour played out and no deck is left")
        hot_trace = lab.trace()
        hot_onset = hot_trace.onset(FIRST_TONE, after=hot_created - 0.5)
        self.assertIsNotNone(hot_onset)
        hot_first_audio = hot_onset - hot_created
        hot_item = items1[0].id
        self.assertLessEqual(air[hot_item]["boundary"], air[hot_item]["commit"])
        self.assertLessEqual(air[hot_item]["commit"], hot_onset, "no PlayEvent before the item's own audio")
        self.assertLessEqual(hot_onset - air[hot_item]["commit"], MAX_COMMIT_TO_PROGRAM_S)
        worst, cv, frames = pcm_integrity(hot_trace, FIRST_TONE, hot_onset + 0.1)
        self.assertEqual(frames, 100)
        self.assertLessEqual(worst, MAX_RESIDUAL_PCT, "hot start: clean program PCM")
        self.assertLessEqual(cv, MAX_AMPLITUDE_CV_PCT)

        # A real zero-deck idle: the engine's own exhausted retry keeps polling; no deck pad exists.
        begin = self._sample()
        self.assertEqual(begin["deck_pads"], 0)
        samples = []
        waited = 0.0
        while waited < IDLE_S:
            pump(1.0)
            waited += 1.0
            samples.append(self._sample())
        self.assertTrue(all(s["deck_pads"] == 0 for s in samples))
        idle_buffers = samples[-1]["deck_buffers"] - begin["deck_buffers"]
        idle_lag = max(s["deck_lag_s"] for s in samples)
        self.assertGreaterEqual(idle_buffers, MIN_IDLE_BUFFERS_PER_S * IDLE_S,
                                "the deck mixer keeps producing (silence) while no deck exists")
        self.assertLessEqual(idle_lag, MAX_IDLE_LAG_S, "the deck mixer's timeline keeps pace with running time")
        silent = lab.trace()
        self.assertEqual(silent.present(FIRST_TONE, begin["running_s"] + 0.5, samples[-1]["running_s"]), 0.0)
        self.assertEqual(silent.present(NEXT_TONE, begin["running_s"], samples[-1]["running_s"]), 0.0)

        # The next hour now exists; the engine's own retry starts its first item.
        available["next"] = True
        deck = None
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline and deck is None:
            pump(0.05)
            deck = next((d for d in engine.decks.values() if d is not None), None)
        self.assertIsNotNone(deck, "the engine's own retry starts the next hour")
        created_at = lab.now()
        created = self._sample()
        rows = [created]
        for _n in range(6):
            pump(1.0)
            rows.append(self._sample())
        rates = [(b["deck_end_s"] - a["deck_end_s"]) / (b["running_s"] - a["running_s"])
                 for a, b in zip(rows, rows[1:])]
        trace = lab.trace()
        onset = trace.onset(NEXT_TONE, after=created_at - 0.5)
        item = deck.log_item.id

        self.assertLessEqual(max(rates), MAX_PROGRESSION_RATE, f"no catch-up burst: {rates}")
        self.assertLessEqual(abs(rows[-1]["deck_lag_s"]), MAX_IDLE_LAG_S)
        self.assertGreater(deck.media_buffer_count, 0, "the item's media decodes")
        self.assertTrue(deck.air_start_recorded, "and airs")
        self.assertIsNotNone(onset, "its audio reaches the program")
        first_audio = onset - created_at
        self.assertLessEqual(first_audio, MAX_FIRST_AUDIO_S)
        self.assertLessEqual(first_audio, hot_first_audio + ONSET_MARGIN_OVER_HOT_START_S,
                             "no slower than a start in a hot pipeline")
        self.assertGreaterEqual(trace.present(NEXT_TONE, created_at, created_at + 5.5), MIN_TONE_PRESENT_S)
        # Accounting: one authoritative claim and one PlayEvent, committed only after the first-real boundary.
        self.assertEqual(PlayEvent.objects.filter(log_item_id_snapshot=item).count(), 1)
        self.assertEqual(LogItem.objects.filter(pk=item, played_at__isnull=False).count(), 1)
        self.assertEqual(LogItem.objects.filter(pk=item, playback_claimed_at__isnull=False).count(), 1)
        self.assertIn("boundary", air.get(item, {}))
        self.assertGreaterEqual(air[item]["commit"], air[item]["boundary"])
        # The SAME generation's audio (its own frequency) reaches the program within a bounded window after commit.
        self.assertLessEqual(air[item]["commit"], onset, "no PlayEvent before the item's own audio")
        self.assertLessEqual(onset - air[item]["commit"], MAX_COMMIT_TO_PROGRAM_S)
        # Raw PCM integrity of that audio (no holes, steps or corrupted samples).
        worst, cv, frames = pcm_integrity(trace, NEXT_TONE, onset + 0.1)
        self.assertEqual(frames, 100)
        self.assertLessEqual(worst, MAX_RESIDUAL_PCT, "post-idle start: clean program PCM")
        self.assertLessEqual(cv, MAX_AMPLITUDE_CV_PCT)
        # One deck-bus format from startup through both hours and the idle (Phase A2).
        self.assertTrue(caps_seen, "the deck mixer negotiated its output")
        self.assertEqual(set(caps_seen), {("S16LE", self.rate, 2, "interleaved")}, f"deck-bus caps: {caps_seen}")
        self.assertEqual(engine._queue_cursor, len(engine.log_items), "the occurrence advanced the queue once")
        connections.close_all()


class ZeroDeckIdle44100Tests(_ZeroDeckIdle):
    rate = 44100


class ZeroDeckIdle48000Tests(_ZeroDeckIdle):
    rate = 48000


del _ZeroDeckIdle
