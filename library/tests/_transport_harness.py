"""Roadmap 2.23 Track A -- shared real-topology transport harness.

Not a test module (no ``test`` prefix): imported by the Eject/VT safety and
Start Next engine tests. Builds a hardware-free PlaybackEngine via
object.__new__ the same way test_engine_deck_lifecycle does, but keeps the
REAL queue path (_start_next_track -> _next_queue_item -> _create_deck), the
REAL Eject/VT state machine and the real GLib timers. Program topology:

    deck bins -> mixer -> vt_duck_gain -> duck_gain -+
    VT fires  -> fx_submix (+ live silence) ---------+-> master_mixer -> sink
                 live silence bed (mic stand-in) ----+

The sink is either a clock-synchronised fakesink (state-machine tests: decks
play in real time so overlaps and fades have duration) or an appsink that
records every program buffer for audio (RMS/tone) assertions. Database work
(claims, request scheduling, PlayEvent accounting) is stubbed here; the
accounting has its own PostgreSQL-backed tests.
"""

from __future__ import annotations

import math
import struct
import tempfile
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

import library.services.engine as eng_module
from library.services import audio_recovery
from library.services.engine import PlaybackEngine


Gst.init(None)

RATE = 44100


def pump(seconds):
    """Iterate the default GLib context for ``seconds`` of wall time."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.002)


def wait_until(predicate, timeout=5.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.002)
    return False


def write_tone_wav(path, *, seconds, freq=440.0, amplitude=0.4, lead_silence=0.0):
    """Stereo 16-bit WAV: optional leading silence, then a sine tone."""
    lead = int(RATE * lead_silence)
    frames = int(RATE * seconds)
    peak = int(32767 * amplitude)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(RATE)
        chunk = []
        for index in range(lead + frames):
            if index < lead or freq <= 0:
                value = 0
            else:
                value = int(peak * math.sin(2 * math.pi * freq * (index - lead) / RATE))
            chunk.append(struct.pack("<hh", value, value))
            if len(chunk) >= 8192:
                output.writeframes(b"".join(chunk))
                chunk = []
        if chunk:
            output.writeframes(b"".join(chunk))


def make_track(path, track_id, *, duration, cue_in=0.0, next_start=None,
               outro_starts=None, title=None):
    track = MagicMock(name=f"track-{track_id}")
    track.id = track_id
    track.filepath = str(path)
    track.title = title or f"Track {track_id}"
    track.duration_seconds = duration
    track.next_start_seconds = duration if next_start is None else next_start
    track.cue_in_seconds = cue_in
    track.outro_starts_seconds = outro_starts
    track.intro_until_seconds = 0.0
    track.play_count = 0
    track.record_label = ""
    track.isrc = ""
    track.format = "wav"
    track.category = None
    track.artist = MagicMock(name="artist")
    track.artist.name = "Harness Artist"
    track.album = MagicMock(name="album")
    track.album.title = "Harness Album"
    return track


def make_item(track, item_id):
    item = MagicMock(name=f"log-item-{item_id}")
    item.id = item_id
    item.playlist_log_id = None
    item.track = track
    item.category_id = None
    item.category = None
    item.position = item_id
    item.played_at = None
    return item


class TransportHarness:
    """One engine plus its media, queue and recording hooks."""

    def __init__(self, testcase, *, appsink=False, sync=True):
        self.testcase = testcase
        self.temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-2.23-transport.")
        testcase.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.on_log_exhausted_calls = []
        self.program_buffers = []   # (running_seconds, left-channel float samples)
        self.claims = []            # LogItem ids in the order decks claimed them
        self.engine = self._build_engine(appsink=appsink, sync=sync)
        testcase.addCleanup(self._cleanup)
        self._patches = [
            patch.object(eng_module, "emit_event", new=lambda **_kwargs: None),
            patch.object(eng_module, "maybe_schedule_song_request", new=lambda _item: None),
            patch.object(eng_module, "_active_message_candidate", new=lambda _track: False),
        ]
        for mocked in self._patches:
            mocked.start()
            testcase.addCleanup(mocked.stop)

    # -- construction -----------------------------------------------------
    def _build_engine(self, *, appsink, sync):
        engine = object.__new__(PlaybackEngine)
        engine._lock = threading.RLock()
        engine.running = True
        engine.main_pipeline = Gst.Pipeline.new("transport-2-23-test")
        engine.mixer = Gst.ElementFactory.make("audiomixer", "mixer")
        engine.vt_duck_gain = Gst.ElementFactory.make("volume", "vt_duck_gain")
        engine.duck_gain = Gst.ElementFactory.make("volume", "duck_gain")
        # Production joins the deck bus, the FX/VT submix and the live mic
        # silence beds in later mixers; live silence makes the program
        # clock-driven even with no deck. Mirror that (one master mixer).
        engine.master_mixer = Gst.ElementFactory.make("audiomixer", "master_mixer")
        engine.fx_submix = Gst.ElementFactory.make("audiomixer", "fx_submix")
        convert = Gst.ElementFactory.make("audioconvert", "program_convert")
        caps = Gst.ElementFactory.make("capsfilter", "program_caps")
        caps.set_property("caps", Gst.Caps.from_string(
            f"audio/x-raw,format=F32LE,rate={RATE},channels=2"))
        if appsink:
            sink = Gst.ElementFactory.make("appsink", "program_sink")
            sink.set_property("sync", sync)
            sink.set_property("emit-signals", True)
            sink.connect("new-sample", self._on_sample)
        else:
            sink = Gst.ElementFactory.make("fakesink", "program_sink")
            sink.set_property("sync", sync)
        for element in (engine.mixer, engine.vt_duck_gain, engine.duck_gain,
                        engine.master_mixer, engine.fx_submix, convert, caps, sink):
            engine.main_pipeline.add(element)
        engine.mixer.link(engine.vt_duck_gain)
        engine.vt_duck_gain.link(engine.duck_gain)
        engine.duck_gain.get_static_pad("src").link(
            engine.master_mixer.request_pad_simple("sink_%u"))
        engine.fx_submix.get_static_pad("src").link(
            engine.master_mixer.request_pad_simple("sink_%u"))
        for target in (engine.master_mixer, engine.fx_submix):
            bed = Gst.ElementFactory.make("audiotestsrc", None)
            bed.set_property("wave", "silence")
            bed.set_property("is-live", True)
            bed_caps = Gst.ElementFactory.make("capsfilter", None)
            bed_caps.set_property("caps", Gst.Caps.from_string(
                f"audio/x-raw,rate={RATE},channels=2"))
            engine.main_pipeline.add(bed)
            engine.main_pipeline.add(bed_caps)
            bed.link(bed_caps)
            bed_caps.get_static_pad("src").link(target.request_pad_simple("sink_%u"))
        # Fixed 10 ms program buffers (441 frames): whole-bin Goertzel
        # analysis at multiples of 100 Hz in tone_level/tone_series.
        engine.master_mixer.set_property("output-buffer-duration", 10 * Gst.MSECOND)
        engine.master_mixer.link(convert)
        convert.link(caps)
        caps.link(sink)

        bus = engine.main_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", engine._on_main_bus_error)
        engine.pipeline_sample_rate = RATE
        engine.decks = {"A": None, "B": None}
        engine._deck_bin_map = {}
        engine._deck_generation_serial = 0
        engine._quarantined_seek_generations = 0
        engine._deck_teardowns = {
            slot: audio_recovery.BoundedTeardownCoordinator(
                f"transport-2-23-{slot.lower()}", timeout_s=1.0, queue_capacity=16
            )
            for slot in ("A", "B")
        }
        engine._deck_watchdog_times = eng_module.deque()
        engine._mic_bin = None
        engine._output_slots = {}
        engine._resume_hint = None
        engine._resume_item = None
        engine.restart_required = False
        engine._poison_skip_identities = []
        engine._poison_reported_identities = set()
        engine.loop = MagicMock()
        engine._engine_session_id = "harness-engine-instance"
        engine._start_next_active = None
        engine._start_next_restoring = None

        # Queue.
        engine.log_items = []
        engine._queue_cursor = 0
        engine._forced_next_items = []
        engine.current_log = None
        engine.__dict__["_active_message_held"] = {}
        engine._roll_over_to_next_hour = lambda: False
        engine._peek_next_hour = lambda: None
        engine._try_extend_live_log_async = lambda: None
        engine._on_log_exhausted = self._record_log_exhausted
        engine._maybe_insert_dedication_intro = lambda item: item
        engine._note_active_message_discard = lambda _item: None
        engine._report_active_message_left = lambda *_args, **_kwargs: None

        # Automation / mic / VT.
        engine.manual_mode = False
        engine._manual_from_mic = False
        engine._manual_hold_pending = False
        engine._next_triggered = False
        engine.mic_live = False
        engine.remote_dj_session = None
        engine.dj_slots = []
        engine._vt = {"phase": "idle"}
        engine._vt_lock = threading.Lock()
        engine._fx_lock = threading.Lock()
        engine._fx_fires = {}
        engine._fx_next_id = 1

        # Presentation and DB seams (see test_engine_deck_lifecycle).
        engine._write_now_playing = lambda _track: None
        engine._write_rbds_category_state = lambda _track: None

        def claim_fixture_occurrence(item):
            item.playback_claimed_at = eng_module.timezone.now()
            self.claims.append(item.id)
            return True

        engine._claim_playback_occurrence = claim_fixture_occurrence
        engine._schedule_occurrence_air_start_from_probe = lambda _deck: None
        engine._persist_deck_duration = self._record_duration
        self.duration_closes = []
        engine._schedule_continuation_segment_start = (
            lambda deck, reason: deck.activate_duration_segment(eng_module.timezone.now(), reason)
        )
        engine._write_state = lambda *_args, **_kwargs: None
        return engine

    def _record_log_exhausted(self, slot):
        self.on_log_exhausted_calls.append(slot)

    def _record_duration(self, deck, *, close_segment=False, occurrence_terminal=False,
                         termination_reason="", interrupted=False):
        if close_segment:
            self.duration_closes.append(
                (deck.log_item.id, deck.generation, termination_reason, occurrence_terminal)
            )
            # Production persists this segment in a PostgreSQL transaction
            # between scheduling the retired bin's NULL and the caller's next
            # step; a small WAV bin's NULL normally completes inside that
            # window. Model the common case deterministically: return only
            # once the slot's bounded teardown has finished.
            coordinator = self.engine._deck_teardowns.get(deck.slot)
            deadline = time.monotonic() + 1.0
            while coordinator is not None and time.monotonic() < deadline:
                snapshot = coordinator.snapshot()
                if snapshot["active_generation"] is None and not snapshot["queue_depth"]:
                    break
                time.sleep(0.001)
        return True

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        segment = sample.get_segment()
        running = segment.to_running_time(Gst.Format.TIME, buffer.pts)
        ok, info = buffer.map(Gst.MapFlags.READ)
        if ok:
            try:
                values = struct.unpack(f"<{info.size // 4}f", info.data)
            finally:
                buffer.unmap(info)
            self.program_buffers.append((running / Gst.SECOND, values[0::2]))
        return Gst.FlowReturn.OK

    def _cleanup(self):
        engine = self.engine
        engine.running = False
        for coordinator in engine._deck_teardowns.values():
            coordinator.stop()
        engine.main_pipeline.set_state(Gst.State.NULL)
        for coordinator in engine._deck_teardowns.values():
            worker = coordinator._worker
            if worker is not None:
                worker.join(timeout=5.0)

    # -- media / queue ------------------------------------------------------
    def media(self, name, *, seconds, freq=440.0, lead_silence=0.0, amplitude=0.4):
        path = self.root / f"{name}.wav"
        if not path.exists():
            write_tone_wav(path, seconds=seconds, freq=freq,
                           lead_silence=lead_silence, amplitude=amplitude)
        return path

    def queue(self, *specs):
        """specs: dicts with id, seconds and optional freq/cue_in/next_start/
        outro_starts/lead_silence. Replaces the engine's queue."""
        items = []
        for spec in specs:
            seconds = spec.get("seconds", 8.0)
            lead = spec.get("lead_silence", 0.0)
            freq = spec.get("freq", 440.0)
            path = self.media(
                f"tone-{seconds}s-{freq}hz-{lead}lead-{spec.get('amplitude', 0.4)}",
                seconds=seconds, freq=freq,
                lead_silence=lead, amplitude=spec.get("amplitude", 0.4),
            )
            track = make_track(
                path, spec.get("track_id", 1000 + spec["id"]), duration=seconds + lead,
                cue_in=spec.get("cue_in", 0.0), next_start=spec.get("next_start"),
                outro_starts=spec.get("outro_starts"),
            )
            items.append(make_item(track, spec["id"]))
        self.engine.log_items = items
        self.engine._queue_cursor = 0
        return items

    def play(self):
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        if not getattr(self, "_ticks_installed", False):
            # The same periodic GLib ticks run() installs: gated seeks and
            # bounded deck teardown advance only through them.
            self._ticks_installed = True
            sources = [
                GLib.timeout_add(eng_module.DECK_SEEK_TICK_MS, self._tick, "_deck_seek_tick"),
                GLib.timeout_add(100, self._tick, "_deck_teardown_tick"),
            ]
            self.testcase.addCleanup(
                lambda: [GLib.source_remove(source) for source in sources
                         if GLib.MainContext.default().find_source_by_id(source)])

    def _tick(self, name):
        if not self.engine.running:
            return False
        getattr(self.engine, name)()
        return True

    # -- observation --------------------------------------------------------
    def occupants(self):
        return {
            slot: (deck.log_item.id if deck is not None else None)
            for slot, deck in self.engine.decks.items()
        }

    def live_ids(self):
        return sorted(
            deck.log_item.id for deck in self.engine.decks.values()
            if deck is not None and not deck.paused and not deck.finished
            and not deck.retirement_started
        )

    def tone_level(self, start, end, freq):
        """Mean single-bin (Goertzel) amplitude of ``freq`` in program
        output between running times ``start`` and ``end`` seconds."""
        values = []
        for running, samples in self.program_buffers:
            if start <= running < end and samples:
                values.append(_goertzel_amplitude(samples, freq))
        return sum(values) / len(values) if values else None

    def tone_series(self, freq):
        return [
            (running, _goertzel_amplitude(samples, freq))
            for running, samples in self.program_buffers if samples
        ]


def running_seconds(engine):
    clock = engine.main_pipeline.get_clock()
    return (clock.get_time() - engine.main_pipeline.get_base_time()) / Gst.SECOND


def start_next_command(engine, *, fade_ms=400, expected="auto", release_manual=False,
                       request_id=None, client_request_id=None, issued_at=None,
                       engine_instance="auto", user_id=1):
    import uuid as _uuid
    if expected == "auto":
        expected = {"live_log_item_ids": sorted(
            deck.log_item.id for deck in engine._live_decks())}
    return {
        "command": "start_next",
        "request_id": request_id or _uuid.uuid4().hex,
        "client_request_id": client_request_id or _uuid.uuid4().hex,
        "user_id": user_id,
        "issued_at": time.time() if issued_at is None else issued_at,
        "engine_instance": (engine._engine_session_id if engine_instance == "auto"
                            else engine_instance),
        "expected": expected,
        "fade_ms": fade_ms,
        "release_manual": release_manual,
    }


def attached_volume_bindings(pad):
    """The pad's ACTUAL attached binding list (disabled ones included) and the
    binding currently attached for ``volume``."""
    return len(pad.control_bindings), pad.get_control_binding("volume")


def _goertzel_amplitude(samples, freq):
    count = len(samples)
    if count == 0:
        return 0.0
    k = int(0.5 + count * freq / RATE)
    omega = 2.0 * math.pi * k / count
    coeff = 2.0 * math.cos(omega)
    s_prev = s_prev2 = 0.0
    for sample in samples:
        s = sample + coeff * s_prev - s_prev2
        s_prev2, s_prev = s_prev, s
    power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    return 2.0 * math.sqrt(max(power, 0.0)) / count


def fake_voicetrack(path, duration, gain_db=0.0):
    audio = SimpleNamespace(path=str(path), duration_seconds=duration)
    return SimpleNamespace(gain_db=gain_db, playable_audio=lambda: audio,
                           filepath=str(path), duration_seconds=duration)


class _VTQuery:
    def __init__(self, result):
        self._result = result

    def select_related(self, *_args):
        return self

    def first(self):
        return self._result


def patch_voicetracks(testcase, *, outro=None, intro=None, duck_db=-6.0,
                      ramp_ms=40, gap_ms=60):
    """VoiceTrack/VoiceTrackConfig/SongRequest lookups used by
    _vt_maybe_enter, without a database."""

    def vt_filter(**kwargs):
        return _VTQuery(outro if kwargs.get("position") == "outro" else intro)

    config = SimpleNamespace(program_duck_db=duck_db, duck_ramp_ms=ramp_ms, min_gap_ms=gap_ms)
    patches = [
        patch.object(eng_module.VoiceTrack.objects, "filter", new=vt_filter),
        patch.object(eng_module.VoiceTrackConfig, "load", new=lambda: config),
        patch.object(eng_module.SongRequest.objects, "filter",
                     new=lambda **_kwargs: SimpleNamespace(exists=lambda: False)),
        patch.object(eng_module, "_close_old_connections", new=lambda: None),
    ]
    for mocked in patches:
        mocked.start()
        testcase.addCleanup(mocked.stop)
    return config
