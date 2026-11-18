"""Roadmap 2.25 B3/B4 -- integrated VT transition lab (not a test module).

The REAL engine transition path -- the production 250 ms ``_poll_position``
tick, ``_start_next_track`` -> ``_next_queue_item`` -> ``_create_deck`` (claim
seam), the B3/B4 transition coordinator (``_vtt_*``), the B2 scheduled fires,
scheduled decks and duck curve -- in the r0144 program topology of the 2.23
transport harness, with the program PCM recorded by the B0 lab (sample-
accurate onset/end of tone-identified signals). Database work is replaced by
explicit seams here; the PostgreSQL behaviour has its own TransactionTestCase.

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

from gi.repository import GLib

import library.services.engine as eng_module
from library.services import vt_audio, vt_transition
from library.tests._transport_harness import _VTQuery, make_item, make_track
from library.tests._vt_b0_lab import NS, Lab, pump, wait_until

# Tone identities (whole multiples of 100 Hz; 10 ms Goertzel bins).
OUT, MUSIC, VOCAL, SPEECH, HEAD, TAIL, MIC = 400, 600, 1400, 1000, 2200, 2600, 3000
OUTRO_SPEECH, URGENT, REQUEST, DEDICATION, FOREIGN = 900, 300, 1100, 800, 1700
MARK = 0.150
TOL = 0.015
CONFIGS = ((44100, "wav"), (44100, "flac"), (48000, "wav"), (48000, "flac"))
POLL_STUBS = ("_check_commands", "_check_stuck_decks", "_reload_queue_if_changed",
              "_sweep_active_message_queue", "_warm_track_cache")


def vt_segments(total, speech=SPEECH):
    """150 ms head marker, speech tone, 150 ms tail marker (both ends measurable)."""
    return [(MARK, HEAD), (total - 2 * MARK, speech), (MARK, TAIL)]


def fake_voicetrack(path, duration, gain_db=0.0):
    audio = SimpleNamespace(path=str(path), duration_seconds=duration)
    return SimpleNamespace(gain_db=gain_db, playable_audio=lambda: audio,
                           filepath=str(path), duration_seconds=duration, media_id=None)


def unplayable_voicetrack():
    return SimpleNamespace(gain_db=0.0, playable_audio=lambda: None, filepath="",
                           duration_seconds=None, media_id=None)


class IntegrationLab:
    """One engine, its queue, VoiceTrack table and recorders."""

    def __init__(self, testcase, *, rate=44100, duck_db=-6.0, ramp_ms=100, gap_ms=200, mic=False):
        self.testcase = testcase
        self.rate = rate
        self.lab = Lab(testcase, rate=rate, mic_freq=MIC if mic else None)
        self.engine = self.lab.engine
        self.root = self.lab.root
        self.table = {}             # (track_id, position) -> fake VoiceTrack
        self.lookups = []           # (track_id, position) in lookup order
        self.air_starts = []        # dicts
        self.now_playing = []       # (track_id, running)
        self.claims = self.lab.h.claims
        self.config = SimpleNamespace(program_duck_db=duck_db, duck_ramp_ms=ramp_ms, min_gap_ms=gap_ms)
        self.start_calls = []       # (running, start kind)
        self._patch()
        self._seams()

    # -- seams ---------------------------------------------------------------
    def _patch(self):
        lab = self

        def vt_filter(**kwargs):
            key = (getattr(kwargs.get("track"), "id", None), kwargs.get("position"))
            lab.lookups.append(key)
            return _VTQuery(lab.table.get(key))

        for mocked in (
            patch.object(eng_module.VoiceTrack.objects, "filter", new=vt_filter),
            patch.object(eng_module.VoiceTrackConfig, "load", new=lambda: lab.config),
            patch.object(eng_module.SongRequest.objects, "filter",
                         new=lambda **_kwargs: SimpleNamespace(exists=lambda: False)),
            patch.object(eng_module, "_close_old_connections", new=lambda: None),
            patch.object(eng_module.DuckingConfig, "load",
                         new=lambda: SimpleNamespace(ptt_auto_manual_enabled=True)),
        ):
            mocked.start()
            self.testcase.addCleanup(mocked.stop)

    def _seams(self):
        engine = self.engine
        lab = self

        def record_air_start(deck, air_started_at=None):
            lab.air_starts.append({"slot": deck.slot, "generation": deck.generation,
                                   "log_item_id": deck.log_item.id, "air_started_at": air_started_at,
                                   "running": lab.now(), "scheduled": deck.scheduled_admission_ns})
        engine._schedule_occurrence_air_start_from_probe = record_air_start
        engine._write_now_playing = lambda track: lab.now_playing.append((track.id, lab.now()))
        original = engine._start_next_track

        def spy(slot=None):
            lab.start_calls.append((lab.now(), engine.__dict__.get("_vtt_start_kind")))
            return original(slot=slot)
        engine._start_next_track = spy

    def poll(self):
        """Install the production position poll (minus DB/IPC side work)."""
        for name in POLL_STUBS:
            setattr(self.engine, name, lambda *_args, **_kwargs: None)
        source = GLib.timeout_add(eng_module.POSITION_POLL_MS, self.engine._poll_position)

        def _remove():
            if GLib.MainContext.default().find_source_by_id(source):
                GLib.source_remove(source)
        self.testcase.addCleanup(_remove)

    def play(self):
        self.lab.play()

    # -- clock / trace ---------------------------------------------------------
    def now(self):
        return self.lab.now()

    def now_ns(self):
        return self.lab.now_ns()

    def trace(self):
        return self.lab.trace()

    # -- media / queue -----------------------------------------------------------
    def media(self, name, segments, *, fmt="wav", media_rate=None):
        return self.lab.media(name, segments, fmt=fmt, media_rate=media_rate or self.rate)

    def vt(self, name, total, *, fmt="wav", media_rate=None, speech=SPEECH):
        return self.media(name, vt_segments(total, speech), fmt=fmt, media_rate=media_rate)

    def item(self, item_id, segments, *, track_id=None, fmt="wav", media_rate=None, cue_in=0.0,
             next_start=None, outro_starts=None, intro_until=0.0, name=None):
        duration = round(sum(seconds for seconds, _f in segments), 6)
        path = self.media(name or f"item-{item_id}-{track_id}", segments, fmt=fmt, media_rate=media_rate)
        track = make_track(path, track_id or 1000 + item_id, duration=duration, cue_in=cue_in,
                           next_start=next_start, outro_starts=outro_starts)
        track.intro_until_seconds = intro_until
        item = make_item(track, item_id)
        item.playback_claimed_at = None      # unclaimed (a MagicMock attribute would read as claimed)
        return item

    def queue(self, *items):
        self.engine.log_items = list(items)
        self.engine._queue_cursor = 0
        return list(items)

    def attach(self, item, position, voicetrack):
        self.table[(item.track.id, position)] = voicetrack

    # -- observation -------------------------------------------------------------------
    def record(self):
        return self.engine.__dict__.get("_vtt")

    def history(self):
        return list(self.engine.__dict__.get("_vtt_history", ()))

    def last_record(self):
        current = self.record()
        if current is not None:
            return current
        history = self.history()
        return history[-1] if history else None

    def media_zero(self, deck):
        return self.engine._vtt_media_zero_ns(deck) / NS

    def deck_of(self, item_id):
        for deck in self.engine.decks.values():
            if deck is not None and deck.log_item.id == item_id:
                return deck
        return None

    def duck_value(self):
        return self.engine.vt_duck_gain.get_property("volume")

    def curve_value(self):
        curve = self.engine.__dict__.get("_vt2_duck")
        return 1.0 if curve is None else curve.value_at(self.now_ns())

    def settle(self, seconds=0.4):
        pump(seconds)
        coordinator = self.engine.__dict__.get("_vt2_teardown")
        deadline = time.monotonic() + 3.0
        while coordinator is not None and time.monotonic() < deadline:
            snap = coordinator.snapshot()
            if snap.get("active_generation") is None and not snap.get("queue_depth"):
                break
            pump(0.05)

    def start(self, slot="A"):
        with self.engine._vtt_starting(None):
            self.engine._start_next_track(slot=slot)
        deck = self.engine.decks.get(slot)
        assert wait_until(lambda: len(self.lab.h.program_buffers) > 0, 2.0)
        return deck

    def mic(self, live):
        self.engine.mic_live = live
        self.engine._apply_mic_mode_hold()

    def fires(self, record=None):
        record = record or self.last_record()
        return [] if record is None else record.fires()

    def event_names(self, record=None):
        record = record or self.last_record()
        return [] if record is None else [name for _t, name, _d in record.events]


def report(label, **values):
    rendered = " ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in values.items())
    print(f"\nVT-METRIC {label} {rendered}", flush=True)


def first(iterable, default=None):
    return next(iter(iterable), default)


__all__ = [
    "CONFIGS", "DEDICATION", "FOREIGN", "HEAD", "IntegrationLab", "MARK", "MIC", "MUSIC", "NS", "OUT",
    "OUTRO_SPEECH", "REQUEST", "SPEECH", "TAIL", "TOL", "URGENT", "VOCAL", "fake_voicetrack", "first",
    "pump", "report", "unplayable_voicetrack", "vt_audio", "vt_segments", "vt_transition", "wait_until",
]
