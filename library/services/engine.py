import json
import os
import signal
import socket
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GLib, GstSdp, GstWebRTC

import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "isadoraair.settings")
django.setup()

from django.db import close_old_connections
from django.db.models import F
from django.utils import timezone
from hardware.models import AudioInput, AudioOutput, AudioPipeline, DuckingConfig, RemoteDJAudioInput
from library.models import Category, LogItem, PlaylistLog, RemoteDJConfig, Track
from library.services.log_builder import (
    DURATION_FIT_MARGIN,
    append_fill_items,
    build_hour_log,
    fill_remaining_hour,
)
from library.services.remote_dj_signaling import RemoteDJSignalingServer

STUDIO_MONITOR_NAME = "Studio Monitor"
STUDIO_MONITOR_FALLBACK_DEVICE = "plughw:2,0"

# StereoTool bridge — a raw (pre-AGC) tap off the mixer, fed to an ALSA
# loopback device StereoTool reads from separately. Unlike Studio Monitor,
# there's no fallback device: if this row has nothing configured, the tee
# branch simply isn't built at all (see _build_main_pipeline).
STEREOTOOL_OUTPUT_NAME = "Stereotool Input"

# Studio mic input, mixed in via a second (master) mixer downstream of
# the deck mixer -- see _build_main_pipeline's duck_gain/master_mixer
# split. No fallback device, same reasoning as StereoTool: if unset, the
# mic bin simply isn't built at all.
MIC_INPUT_NAME = "Studio Microphone 1"

DUCK_RAMP_MS = 500
DUCK_RAMP_STEPS = 20  # ~25ms per step -- smooth enough for a loudness fade, not sample-accurate automation

STATE_PATH = Path("/run/isadoraair/engine_state.json")
CMD_PATH = Path("/run/isadoraair/engine_cmd.json")
NOW_PLAYING_PATH = Path("/run/isadoraair/now_playing.json")

# Pre-processor VU meter: values are updated by GStreamer's `level`
# element sitting on the summed master output (post-mix, post-duck,
# post-mic; PRE-AGC and PRE-StereoTool). Written to LEVELS_PATH at
# LEVEL_INTERVAL_MS cadence for the dashboard's fast-cadence poll. Atomic
# rename on each write so a mid-write read never sees a truncated file.
LEVELS_PATH = Path("/run/isadoraair/levels.json")
LEVELS_TMP_PATH = Path("/run/isadoraair/levels.json.tmp")
LEVEL_INTERVAL_MS = 50
LEVEL_PEAK_TTL_MS = 300
LEVEL_PEAK_FALLOFF_DB_PER_SEC = 20.0
POSITION_POLL_MS = 250
AUTO_BUILD_CHECK_SECONDS = 10
NEXT_HOUR_LOOKAHEAD_SECONDS = 30
CACHE_WARM_LEAD_SECONDS = 3.0
SILENCE_PRIME_SECONDS = 0.3
DECK_STUCK_TIMEOUT_SECONDS = 30  # generous margin past a track's own duration before assuming its EOS was missed
SLOTS = ("A", "B")

# Remote DJ over WebRTC (see /home/jreed/.claude/plans/warm-zooming-rose.md).
# Opus's RTP payload mandates 48kHz per RFC 7587 regardless of
# AudioPipeline.sample_rate -- this is NOT the same as pipeline_sample_rate
# and must not be confused with it.
REMOTE_DJ_OPUS_RATE = 48000
REMOTE_DJ_OPUS_FRAME_SIZE_MS = 10  # over the 20ms default, to shave latency
# Small, leaky-upstream buffer for the monitor-return branch -- this is
# the latency-critical path the whole feature exists for; leaky-upstream
# so it can only ever drop its own data, same reasoning as stereotool_queue.
REMOTE_DJ_MONITOR_QUEUE_MS = 250


class RemoteDJSession:
    """Holds every GStreamer element reference for the one active
    remote-DJ WebRTC session -- mirrors the Deck class's role for a
    playback slot (a thin data holder; the actual lifecycle logic lives
    in PlaybackEngine's _remote_dj_* methods, same split as
    Deck/_create_deck/_remove_deck). "One remote DJ at a time" is a
    confirmed design decision -- self.remote_dj_session is a single slot,
    not a dict/pool."""
    def __init__(self):
        self.webrtc = None
        self.ice_agent = None
        self.remote_gate = None
        self.master_mixer_pad = None
        self.monitor_tee_pad = None
        self.local_mic_tee_pad = None  # only set if local mic exists
        # Every element this session added directly to main_pipeline
        # (monitor-return chain built at session start; inbound
        # depay/dec/convert/resample/queue chain built later, in
        # _remote_dj_on_pad_added, once the remote mic's RTP actually
        # arrives) -- torn down as one list in _remote_dj_session_stop,
        # mirroring _remove_deck's teardown shape.
        self.elements = []
        self.real_buf_seen = False


class Deck:
    def __init__(self, slot, track, log_item, pipeline, mixer_pad, silence_primed=False):
        self.slot = slot
        self.track = track
        self.log_item = log_item
        self.pipeline = pipeline
        self.mixer_pad = mixer_pad
        self.started_at = None
        self.finished = False
        self.paused = False
        self.paused_position = 0.0
        # True for decks built with a silence lead-in (see _create_deck) —
        # query_position() isn't reliable through the internal `concat`
        # segment-switch (can report a frozen/stale value rather than
        # failing cleanly), so these decks track position by wall clock
        # only instead (_get_deck_position).
        self.silence_primed = silence_primed


class PlaybackEngine:
    def __init__(self):
        Gst.init(None)
        self.loop = GLib.MainLoop()
        self.mixer = None
        self.alsasink = None
        self.agc_dynamic = None
        self.agc_makeup = None
        self.agc_limiter = None
        self.stereotool_sink = None
        self.duck_gain = None
        self.master_mixer = None
        self.mic_ptt_valve = None
        self.mic_ptt_volume = None
        self.mic_gain = None
        self.mic_ok = True
        self.mic_live = False
        self.local_mic_tee = None  # only built if remote_dj + local mic both enabled
        self._mic_bin = None
        # Remote DJ over WebRTC. remote_dj_tee only exists at all when
        # RemoteDJConfig.enabled (see _build_main_pipeline) -- while off,
        # these all stay None and the pipeline topology is unchanged from
        # today. remote_dj_session is a single slot, not a dict/pool ("one
        # remote DJ at a time" is a confirmed design decision).
        self.remote_dj_tee = None
        self.remote_dj_session = None
        self._remote_dj_server = None
        # Manual mode: DJ-controlled hold on the auto-crossfade handoff,
        # for talking over a song's outro. The currently-playing deck is
        # never touched -- it always finishes normally -- only the
        # *next* deck's start is withheld. _manual_hold_pending tracks
        # whether a handoff was actually due (trigger point reached, or
        # the leading deck fully finished) while manual_mode was on, so
        # flipping back to Auto only force-starts the next track when a
        # handoff was genuinely waiting -- an early manual-on/manual-off
        # toggle before the trigger point is a no-op, not a hard cut.
        self.manual_mode = False
        self._manual_hold_pending = False
        # True while Manual mode is being held by the mic system (any
        # mic live) rather than by an explicit operator toggle -- so
        # that when the last mic goes quiet, we restore Auto for them.
        # An explicit operator toggle of manual_mode CLEARS this flag,
        # so a manually-selected Manual survives the next mic release.
        self._manual_from_mic = False
        self._duck_ramp_source_id = None
        self.pipeline_sample_rate = None
        self.main_pipeline = None
        self.decks = {"A": None, "B": None}
        self._deck_bin_map = {}
        self._cache_warm_item_id = None
        self._last_queue_reload = 0
        self._next_triggered = False
        self.current_log = None
        self.log_items = []
        self._queue_cursor = 0
        self._next_hour_peek = None
        self._next_hour_peek_at = 0.0
        self._last_live_extend_attempt = 0.0
        self.running = False
        self._position_timer = None
        self._lock = threading.RLock()

    def start(self):
        self.running = True
        self._build_main_pipeline()
        self._load_current_hour_log()

        if not self.log_items:
            print("No approved log for current hour. Waiting...")
        else:
            self._start_next_track()

        self._position_timer = GLib.timeout_add(POSITION_POLL_MS, self._poll_position)
        GLib.timeout_add_seconds(AUTO_BUILD_CHECK_SECONDS, self._ensure_upcoming_logs)

        if RemoteDJConfig.load().enabled:
            self._warm_stun_dns()
            self._remote_dj_server = RemoteDJSignalingServer(self)
            self._remote_dj_server.start()

        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self._handle_signal_glib)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self._handle_signal_glib)

        # Also set Python-level handler as fallback for pre-loop signals
        def _force_quit(signum, frame):
            print("\nForce quit.")
            self.running = False
            try:
                self.loop.quit()
            except Exception:
                pass
            sys.exit(1)
        signal.signal(signal.SIGTERM, _force_quit)
        signal.signal(signal.SIGINT, _force_quit)

        print("Engine started.")
        self.loop.run()
        self.stop()

    def stop(self):
        self.running = False
        if self.remote_dj_session:
            self._remote_dj_session_stop()
        if self.main_pipeline:
            self.main_pipeline.set_state(Gst.State.NULL)
        for deck in self.decks.values():
            if deck and deck.pipeline:
                deck.pipeline.set_state(Gst.State.NULL)
        if self.loop.is_running():
            self.loop.quit()
        self._write_state(transport="STOPPED")
        print("Engine stopped.")

    def _handle_signal_glib(self):
        print("Shutting down...")
        self.loop.quit()
        return GLib.SOURCE_REMOVE

    def _resolve_studio_monitor_device(self):
        try:
            configured = (
                AudioOutput.objects
                .filter(name=STUDIO_MONITOR_NAME)
                .values_list("device", flat=True)
                .first()
            )
        except Exception as exc:
            print(f"  Failed to read AudioOutput config ({exc}); falling back to {STUDIO_MONITOR_FALLBACK_DEVICE}")
            return STUDIO_MONITOR_FALLBACK_DEVICE
        if not configured:
            print(f"  AudioOutput '{STUDIO_MONITOR_NAME}' has no device set; falling back to {STUDIO_MONITOR_FALLBACK_DEVICE}")
            return STUDIO_MONITOR_FALLBACK_DEVICE
        print(f"  AudioOutput '{STUDIO_MONITOR_NAME}' -> {configured}")
        return configured

    def _resolve_stereotool_device(self):
        try:
            configured = (
                AudioOutput.objects
                .filter(name=STEREOTOOL_OUTPUT_NAME)
                .values_list("device", flat=True)
                .first()
            )
        except Exception as exc:
            print(f"  Failed to read AudioOutput config for '{STEREOTOOL_OUTPUT_NAME}' ({exc}); StereoTool bridge disabled")
            return None
        if not configured:
            print(f"  AudioOutput '{STEREOTOOL_OUTPUT_NAME}' has no device set; StereoTool bridge disabled")
            return None
        print(f"  AudioOutput '{STEREOTOOL_OUTPUT_NAME}' -> {configured}")
        return configured

    def _resolve_mic_device(self):
        try:
            configured = (
                AudioInput.objects
                .filter(name=MIC_INPUT_NAME)
                .values_list("device", flat=True)
                .first()
            )
        except Exception as exc:
            print(f"  Failed to read AudioInput config for '{MIC_INPUT_NAME}' ({exc}); mic disabled")
            return None
        if not configured:
            print(f"  AudioInput '{MIC_INPUT_NAME}' has no device set; mic disabled")
            return None
        print(f"  AudioInput '{MIC_INPUT_NAME}' -> {configured}")
        return configured

    def _build_mic_chain(self):
        """Builds the mic capture chain as its own Gst.Bin (not loose
        elements directly in main_pipeline) so a bus watch can be
        scoped to just this bin -- mirrors the deck bins' own
        get_bus()/add_signal_watch()/message::error pattern (see
        _start_next_track/_on_deck_error). Returns [] if no mic device
        is configured; the caller treats an empty list exactly like
        "mic not configured" (self.mic_ptt_valve/self.mic_gain stay
        None). Unlike deck bins (decodebin's src pad appears
        asynchronously after typefind/demux), every element here is
        static, so the whole chain links and gets its ghost pad target
        set immediately -- no deferred pad-added handling needed."""
        mic_device = self._resolve_mic_device()
        if not mic_device:
            return []

        src = Gst.ElementFactory.make("alsasrc", "mic_src")
        src.set_property("device", mic_device)
        convert = Gst.ElementFactory.make("audioconvert", "mic_convert")
        resample = Gst.ElementFactory.make("audioresample", "mic_resample")
        capsfilter = Gst.ElementFactory.make("capsfilter", "mic_caps")
        caps = Gst.Caps.from_string(f"audio/x-raw,rate={self.pipeline_sample_rate},channels=2")
        capsfilter.set_property("caps", caps)

        # alsasrc is a genuinely live, hardware-clocked source feeding
        # directly into audiomixer, unlike filesrc/decodebin's buffered
        # pull-push hybrid -- this queue decouples ALSA capture cadence
        # from the mixer's aggregation timing so a scheduling hiccup on
        # one side doesn't glitch the whole mix. NOT leaky (unlike the
        # StereoTool queue above): the mic feeds the *shared* master
        # mixer, so silently dropping mic audio would be an audible
        # glitch in the primary output, unlike the StereoTool branch
        # which is only ever allowed to drop its own copy.
        queue = Gst.ElementFactory.make("queue", "mic_queue")
        queue.set_property("max-size-time", 200_000_000)  # 200ms
        queue.set_property("max-size-buffers", 0)
        queue.set_property("max-size-bytes", 0)

        self.mic_gain = Gst.ElementFactory.make("volume", "mic_gain")
        self._apply_mic_gain()

        # The PTT gate. Originally a `valve` element (drop=True closed,
        # drop=False open), which fully removed buffers from the stream
        # when closed rather than passing through zero-amplitude ones.
        # That was the "stronger" safety guarantee for a mic-can't-leak
        # requirement -- but in practice it directly caused a much worse
        # failure mode: master_mixer (audiomixer/GstAggregator) needs a
        # first buffer on every linked sink pad before it will output
        # anything downstream, so with the valve dropping mic buffers
        # from a cold start, the aggregator never produced its first
        # output buffer, Studio Monitor's alsasink never prerolled, and
        # the whole studio-monitor chain sat silent from every engine
        # restart until a manual Mic PTT toggle happened to feed a
        # buffer through and unblock it. Confirmed via full state-and-
        # buffer-probe instrumentation on the live pipeline. Replaced
        # with a volume element that just multiplies mic samples by 0
        # (off) or 1 (on) -- audio always flows, aggregator is always
        # satisfied, and volume=0.0 is a rock-solid mute (a
        # multiplication by zero produces exact silence at the sample
        # level, not just attenuated audio). Same failure mode as the
        # valve if the wrong property gets set (drop=False vs
        # volume=1.0), so no real safety regression, just no more dead
        # air on cold start.
        self.mic_ptt_volume = Gst.ElementFactory.make("volume", "mic_ptt_volume")
        self.mic_ptt_volume.set_property("volume", 0.0)
        # Backwards-compat alias -- other places in this file still
        # reference `mic_ptt_valve` (e.g. _on_mic_error's defensive
        # close, _build_main_pipeline's "mic not configured" checks).
        # Rather than rename everywhere and risk missing one, keep the
        # old attribute name pointing at the volume element; both
        # `mic_ptt_valve is None` and `mic_ptt_valve.set_property(...)`
        # continue to work.
        self.mic_ptt_valve = self.mic_ptt_volume

        mic_bin = Gst.Bin.new(f"mic_{int(time.time() * 1000)}")
        for el in (src, convert, resample, capsfilter, queue, self.mic_gain, self.mic_ptt_volume):
            mic_bin.add(el)
        src.link(convert)
        convert.link(resample)
        resample.link(capsfilter)
        capsfilter.link(queue)
        queue.link(self.mic_gain)
        self.mic_gain.link(self.mic_ptt_volume)

        ghost_pad = Gst.GhostPad.new("src", self.mic_ptt_volume.get_static_pad("src"))
        mic_bin.add_pad(ghost_pad)

        # mic_bin is a plain Gst.Bin added into self.main_pipeline, not its
        # own Gst.Pipeline (unlike deck.pipeline) -- a Bin has no bus of its
        # own. Error messages from its children are forwarded up to the
        # containing Pipeline's bus instead, so the watch is set up there
        # (see _build_main_pipeline) and filtered to this bin.
        self._mic_bin = mic_bin

        self.mic_ok = True
        return [mic_bin]

    def _on_element_message(self, bus, message):
        """Element messages from the shared main-pipeline bus. Today only
        `output_level` is watched here (see LEVELS_PATH docstring);
        anything else is ignored so this stays cheap and can't accidentally
        misinterpret a message from a different element that happens to
        share a structure name."""
        structure = message.get_structure()
        if structure is None or structure.get_name() != "level":
            return True
        if message.src is not self.output_level:
            return True
        # rms/peak/decay each come out as a per-channel list of dBFS
        # values. PyGObject exposes the underlying GValueArray as
        # iterable -- list(...) unpacks it to a plain list of floats.
        try:
            rms = list(structure.get_value("rms")) or []
            peak = list(structure.get_value("peak")) or []
            decay = list(structure.get_value("decay")) or []
        except Exception:
            return True
        payload = {
            "ts": time.time(),
            "rms": rms,
            "peak": peak,
            "decay": decay,
        }
        try:
            LEVELS_PATH.parent.mkdir(parents=True, exist_ok=True)
            LEVELS_TMP_PATH.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(LEVELS_TMP_PATH, LEVELS_PATH)
        except Exception:
            # A single failed write is not worth crashing the pipeline
            # over -- the next tick (LEVEL_INTERVAL_MS from now) will
            # try again with fresh values.
            pass
        return True

    def _on_main_bus_error(self, bus, message):
        # self.main_pipeline's bus isn't watched for anything else today
        # (deck errors go through each deck's own dedicated Gst.Pipeline
        # bus instead) -- filter to only react to errors that originate
        # inside the mic bin, so this doesn't swallow/mishandle unrelated
        # pipeline errors.
        if self._mic_bin is None:
            return True
        obj = message.src
        while obj is not None:
            if obj == self._mic_bin:
                return self._on_mic_error(bus, message)
            obj = obj.get_parent()
        return True

    def _on_mic_error(self, bus, message):
        err, debug = message.parse_error()
        print(f"  Mic error: {err} ({debug})")
        # Silence is the safe reaction to a runtime mic fault -- not
        # mixer-pad surgery. This codebase's own seek/resume history
        # already shows unlinking a live-mixer-linked bin mid-flight is
        # the riskier operation, reserved for planned transitions, not
        # error recovery.
        self.mic_ok = False
        self.mic_live = False
        if self.mic_ptt_volume is not None:
            self.mic_ptt_volume.set_property("volume", 0.0)
        return True

    def _apply_mic_gain(self):
        if self.mic_gain is None:
            return
        try:
            gain_db = (
                AudioInput.objects.filter(name=MIC_INPUT_NAME)
                .values_list("gain_db", flat=True).first()
            )
        except Exception as exc:
            print(f"  Failed to read AudioInput gain_db ({exc}); using 0dB")
            gain_db = None
        self.mic_gain.set_property("volume", 10 ** ((gain_db or 0.0) / 20.0))

    def _resolve_pipeline_sample_rate(self):
        try:
            rate = AudioPipeline.load().sample_rate
        except Exception as exc:
            print(f"  Failed to read AudioPipeline config ({exc}); falling back to 48000")
            return 48000
        print(f"  Pipeline sample rate -> {rate}")
        return rate

    def _build_main_pipeline(self):
        self.main_pipeline = Gst.Pipeline.new("isadoraair")
        self.pipeline_sample_rate = self._resolve_pipeline_sample_rate()

        self.mixer = Gst.ElementFactory.make("audiomixer", "mixer")
        # Tried setting `latency`/`min-upstream-latency` here (to give the
        # aggregator more patience for a newly-linked deck's decode delay)
        # — didn't fix the clipping, and introduced a new regression: it
        # also trimmed the *outgoing* deck's tail/outro right around the
        # transition, which never happened before this change. Reverted.
        self.alsasink = Gst.ElementFactory.make("alsasink", "output")
        self.alsasink.set_property("device", self._resolve_studio_monitor_device())

        convert = Gst.ElementFactory.make("audioconvert", "outconvert")
        resample = Gst.ElementFactory.make("audioresample", "outresample")
        capsfilter = Gst.ElementFactory.make("capsfilter", "outcaps")
        caps = Gst.Caps.from_string(f"audio/x-raw,rate={self.pipeline_sample_rate},channels=2")
        capsfilter.set_property("caps", caps)

        # Program-bus attenuation. Sits between capsfilter and the VU
        # meter so a hot summed master (multiple decks + live mic + a
        # remote DJ mic all summing into master_mixer) can't drive
        # StereoTool past 0 dBFS. Read fresh from AudioPipeline at build
        # time; changing the value requires an engine restart, same as
        # sample_rate. A downstream volume adjustment on the Studio
        # Monitor path (agc_makeup) can compensate for the -6dB drop if
        # the operator wants louder studio speakers without touching the
        # pipeline's headroom to StereoTool.
        program_gain_db = AudioPipeline.load().program_gain_db
        self.program_gain = Gst.ElementFactory.make("volume", "program_gain")
        self.program_gain.set_property("volume", 10 ** (program_gain_db / 20.0))

        # Pre-processor VU meter tap. Sits between program_gain and the
        # (tee | agc_dynamic) so it measures the SUMMED master output
        # AFTER the program-bus attenuation -- i.e. exactly the level
        # that StereoTool sees. Post-mix, post-duck, post-mic, but
        # PRE-AGC and PRE-StereoTool. That's what an operator can
        # actually influence (fader levels, mic PTT, ducking depth),
        # which is what a pre-processor VU is for. Emits bus messages
        # every LEVEL_INTERVAL_MS; the handler (_on_element_message)
        # writes them to LEVELS_PATH.
        self.output_level = Gst.ElementFactory.make("level", "output_level")
        self.output_level.set_property("post-messages", True)
        self.output_level.set_property("interval", LEVEL_INTERVAL_MS * Gst.MSECOND)
        self.output_level.set_property("peak-ttl", LEVEL_PEAK_TTL_MS * Gst.MSECOND)
        self.output_level.set_property("peak-falloff", LEVEL_PEAK_FALLOFF_DB_PER_SEC)

        # Interim leveling for the studio monitor only (StereoTool will
        # handle real transmitter processing separately, elsewhere). Kept
        # permanently in the chain rather than conditionally linked, so
        # enabling/disabling is just a property change, never a pipeline
        # topology change — see _apply_agc_config.
        self.agc_dynamic = Gst.ElementFactory.make("audiodynamic", "agc_dynamic")
        self.agc_makeup = Gst.ElementFactory.make("volume", "agc_makeup")
        self.agc_limiter = Gst.ElementFactory.make("rglimiter", "agc_limiter")

        # Ducking + mic mixing: a second (master) mixer sits between the
        # deck mixer and everything downstream (format normalization,
        # StereoTool tap, AGC, Studio Monitor). Ducking the DECKS'
        # combined output specifically -- rather than "lower everything"
        # downstream of where the mic joins -- means a track transition
        # mid-talkover never causes a duck discontinuity: duck_gain only
        # ever sees "whatever the deck mixer already combined," it
        # doesn't care which deck is leading. Both elements are always
        # built, even with no mic/ducking configured -- same "permanent,
        # inert unless engaged" approach already used for the AGC chain
        # below (duck_gain's volume just sits at 1.0 if never triggered;
        # master_mixer happily passes through a single input if no mic
        # is ever linked, exactly like audiomixer already does today
        # with only one deck active).
        self.duck_gain = Gst.ElementFactory.make("volume", "duck_gain")
        self.master_mixer = Gst.ElementFactory.make("audiomixer", "master_mixer")

        elements = [
            self.mixer, self.duck_gain, self.master_mixer, convert, resample, capsfilter,
            self.program_gain, self.output_level,
            self.agc_dynamic, self.agc_makeup, self.agc_limiter,
            self.alsasink,
        ]

        # StereoTool bridge — raw (pre-AGC) tap off the mixer, split via a
        # tee right after format normalization. Only built at all if a
        # device is actually configured; otherwise the topology is
        # unchanged from before (capsfilter links directly to agc_dynamic).
        stereotool_device = self._resolve_stereotool_device()
        self.stereotool_sink = None
        stereotool_tee = None
        stereotool_queue = None
        if stereotool_device:
            stereotool_tee = Gst.ElementFactory.make("tee", "stereotool_tee")
            stereotool_queue = Gst.ElementFactory.make("queue", "stereotool_queue")
            # Leaky upstream (drop oldest buffered data first) so a stalled
            # or not-yet-listening StereoTool bridge — e.g. nothing has
            # opened the other end of the ALSA loopback pair yet — can
            # never back up into this queue and block the tee, which would
            # otherwise stall the shared Studio Monitor/on-air chain too.
            # This branch only ever drops its own audio, nothing else's.
            stereotool_queue.set_property("leaky", 1)
            stereotool_queue.set_property("max-size-time", 1_000_000_000)  # 1s
            stereotool_queue.set_property("max-size-buffers", 0)
            stereotool_queue.set_property("max-size-bytes", 0)
            self.stereotool_sink = Gst.ElementFactory.make("alsasink", "stereotool_output")
            self.stereotool_sink.set_property("device", stereotool_device)
            # Critical: without these, this sink's own preroll (waiting for
            # its first buffer) can block the *entire pipeline's* PAUSED ->
            # PLAYING transition if the leaky queue above ever drops that
            # first buffer — verified this stalls the real pipeline (stuck
            # ASYNC/PAUSED forever, zero audio ever reaching this sink)
            # despite the Studio Monitor branch appearing to work fine.
            # `async=False` means this sink doesn't hold up the pipeline's
            # state changes waiting to preroll; `sync=False` means it just
            # renders buffers as they arrive rather than clock-pacing them
            # against the *other* sink's real hardware clock (Studio
            # Monitor's card vs this ALSA loopback's virtual one).
            self.stereotool_sink.set_property("sync", False)
            self.stereotool_sink.set_property("async", False)
            elements += [stereotool_tee, stereotool_queue, self.stereotool_sink]

        # Mic input — only built if a device is actually configured;
        # otherwise self.mic_ptt_valve/self.mic_gain stay None (same
        # "topology simply narrower" approach as the StereoTool tee
        # above), and _set_mic_ptt() no-ops if ever asked to toggle a
        # mic that isn't there. Wrapped defensively: a configured-but-
        # not-actually-present device (e.g. the Focusrite isn't plugged
        # in yet) must never crash or block the rest of this pipeline
        # from starting -- same "degrade gracefully" philosophy as
        # _resolve_studio_monitor_device/_apply_audio_output_device.
        mic_elements = []
        try:
            mic_elements = self._build_mic_chain()
        except Exception as exc:
            print(f"  Failed to build mic input ({exc}); mic disabled")
            self.mic_ptt_valve = None
            self.mic_ptt_volume = None
            self.mic_gain = None
            self._mic_bin = None
        # Bus signal watch: added once so both the level-metering handler
        # (unconditional -- output_level is always in the pipeline) and the
        # mic error handler (only when a mic bin exists) can subscribe.
        bus = self.main_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::element", self._on_element_message)
        if mic_elements:
            bus.connect("message::error", self._on_main_bus_error)
        elements += mic_elements

        # Remote DJ over WebRTC monitor-return tap -- a tee inserted
        # between duck_gain and master_mixer, built unconditionally
        # whenever the feature is enabled (mirrors stereotool_tee's
        # "always built, optional branch" shape just above), so that
        # tapping here is structurally mix-minus: no mic (local or
        # remote) has joined the signal yet at this point, and the duck
        # is already applied. The on-air branch below is otherwise
        # byte-for-byte the same link that existed before this feature —
        # while RemoteDJConfig.enabled is False, remote_dj_tee stays None
        # and duck_gain links straight to master_mixer exactly as today.
        # A capsfilter is required immediately before the tee (not
        # optional!) -- validated the hard way in the offline harness
        # work: a downstream branch's fixed caps (the monitor-return
        # leg's eventual 48kHz-for-Opus capsfilter) back-propagate
        # through a shared tee and can force the *other* branch to
        # fixate at the same rate, colliding with audiomixer's "first
        # sink pad to negotiate wins the shared rate" rule and silently
        # killing the on-air branch with no bus error. Fixating the
        # tee's own input here means each branch negotiates
        # independently downstream instead.
        self.remote_dj_tee = None
        remote_dj_fixate_caps = None
        if RemoteDJConfig.load().enabled:
            remote_dj_fixate_caps = Gst.ElementFactory.make("capsfilter", "remote_dj_fixate_caps")
            remote_dj_fixate_caps.set_property(
                "caps", Gst.Caps.from_string(f"audio/x-raw,rate={self.pipeline_sample_rate},channels=2"),
            )
            self.remote_dj_tee = Gst.ElementFactory.make("tee", "remote_dj_tee")
            elements += [remote_dj_fixate_caps, self.remote_dj_tee]

        for el in elements:
            self.main_pipeline.add(el)

        self.mixer.link(self.duck_gain)
        duck_pad = self.master_mixer.request_pad_simple("sink_%u")
        if self.remote_dj_tee:
            self.duck_gain.get_static_pad("src").link(remote_dj_fixate_caps.get_static_pad("sink"))
            remote_dj_fixate_caps.link(self.remote_dj_tee)
            onair_pad = self.remote_dj_tee.request_pad_simple("src_%u")
            onair_pad.link(duck_pad)
        else:
            self.duck_gain.get_static_pad("src").link(duck_pad)
        if mic_elements:
            if self.remote_dj_tee is not None:
                # Tee local mic (post-PTT) so a remote-DJ session can also
                # tap it and mix it into the remote's monitor return --
                # this is what lets the studio operator and remote DJ
                # actually converse; without the tap the remote's
                # mix-minus would exclude the local mic too and only
                # carry decks. Requested per session (in
                # _remote_dj_build_session) so its downstream mixer only
                # exists when there's a DJ to feed. Non-leaky and
                # unbounded time-wise: a session can drop and re-add
                # this tap without disturbing the always-live branch to
                # master_mixer.
                self.local_mic_tee = Gst.ElementFactory.make("tee", "local_mic_tee")
                self.main_pipeline.add(self.local_mic_tee)
                mic_elements[-1].link(self.local_mic_tee)
                mic_onair_pad = self.local_mic_tee.request_pad_simple("src_%u")
                mic_pad = self.master_mixer.request_pad_simple("sink_%u")
                mic_onair_pad.link(mic_pad)
            else:
                mic_pad = self.master_mixer.request_pad_simple("sink_%u")
                mic_elements[-1].get_static_pad("src").link(mic_pad)
        self.master_mixer.link(convert)
        convert.link(resample)
        resample.link(capsfilter)

        capsfilter.link(self.program_gain)
        self.program_gain.link(self.output_level)
        if stereotool_tee:
            self.output_level.link(stereotool_tee)
            stereotool_tee.link(self.agc_dynamic)
            stereotool_tee.link(stereotool_queue)
            stereotool_queue.link(self.stereotool_sink)
        else:
            self.output_level.link(self.agc_dynamic)

        self.agc_dynamic.link(self.agc_makeup)
        self.agc_makeup.link(self.agc_limiter)
        self.agc_limiter.link(self.alsasink)

        self._apply_agc_config()

        self.main_pipeline.set_state(Gst.State.PLAYING)

    def _apply_agc_config(self):
        """(Re)apply the studio monitor's AGC settings (fields on its
        AudioOutput row — see hardware/admin.py's "AGC (Studio Monitor
        Leveling)" fieldset) to the already-built pipeline elements.
        `ratio`/`threshold` (audiodynamic) and `volume` are GStreamer
        'controllable' properties — safe to set live while PLAYING, no
        READY-state drop needed. Disabled == unity values, functionally
        identical to not having these elements in the chain at all."""
        close_old_connections()
        cfg = AudioOutput.objects.filter(name=STUDIO_MONITOR_NAME).first()
        enabled = bool(cfg and cfg.agc_enabled)
        if enabled:
            self.agc_dynamic.set_property("ratio", cfg.agc_ratio)
            self.agc_dynamic.set_property("threshold", cfg.agc_threshold)
            self.agc_dynamic.set_property("characteristics", 1 if cfg.agc_soft_knee else 0)
            self.agc_makeup.set_property("volume", 10 ** (cfg.agc_makeup_gain_db / 20.0))
            self.agc_limiter.set_property("enabled", True)
        else:
            self.agc_dynamic.set_property("ratio", 1.0)
            self.agc_makeup.set_property("volume", 1.0)
            self.agc_limiter.set_property("enabled", False)
        print(f"  Applied AGC config: enabled={enabled}"
              + (f" ratio={cfg.agc_ratio} threshold={cfg.agc_threshold} makeup_gain_db={cfg.agc_makeup_gain_db}" if cfg else " (no AudioOutput row)"))

    def _load_current_hour_log(self):
        now = timezone.localtime()
        self._load_log_for(now.date(), now.hour)

        if not self.log_items:
            fallback = (
                PlaylistLog.objects
                .filter(date=now.date(), status="approved", hour__lte=now.hour)
                .order_by("-hour")
                .first()
            )
            if fallback:
                print(f"No log for hour {now.hour}, falling back to hour {fallback.hour}")
                self._load_log_for(fallback.date, fallback.hour)

    def _load_log_for(self, target_date, hour):
        close_old_connections()
        log = (
            PlaylistLog.objects
            .filter(date=target_date, hour=hour, status="approved")
            .first()
        )
        if not log:
            self.current_log = None
            self.log_items = []
            self._queue_cursor = 0
            return

        self.current_log = log
        self.log_items = list(
            log.items
            .select_related("track", "track__artist", "track__album", "track__category", "track__category__kind")
            .order_by("position")
        )
        # Advance past anything already played -- an engine restart
        # mid-hour must NOT replay the log from position 0, or we'd
        # start hearing tracks that already aired. Especially bad for
        # inserted WxAlert / OGRemote urgent tracks, which are
        # positioned in the DB where the queue cursor was at insertion
        # time; every restart would then replay yesterday's severe
        # thunderstorm alert until the hour rolls over. `played_at` is
        # set at the moment a track's deck starts (_create_deck), so
        # "played_at set" = "started airing", which is the right
        # granularity for skip-on-restart (a track that started but was
        # interrupted mid-play is still skipped rather than resumed,
        # which matches the resume-from-next-boundary contract every
        # other restart path already uses).
        self._queue_cursor = 0
        for i, item in enumerate(self.log_items):
            if item.played_at is None:
                self._queue_cursor = i
                break
        else:
            self._queue_cursor = len(self.log_items)
        skipped = self._queue_cursor
        print(f"Loaded log for {target_date} {hour:02d}:00 — {len(self.log_items)} items "
              f"({'resuming at position ' + str(skipped) if skipped else 'from top'})")

    def _ensure_upcoming_logs(self):
        """No human approval step for now — auto-build (and
        auto-approve) whatever hour needs it: the current hour, so a
        freshly-started or catching-up engine has something to play
        right away, and the next hour once we're within the last
        NEXT_HOUR_LOOKAHEAD_SECONDS of the top of the hour, so a late
        schedule-grid edit still has a chance to take effect before
        it's locked in."""
        if not self.running:
            return False

        close_old_connections()
        now = timezone.localtime()

        self._ensure_log_approved(now.date(), now.hour)

        # Checked every tick, not just the tick that built the log —
        # otherwise if the one attempt to start playback right after a
        # build doesn't land (or the log already existed for some
        # other reason, e.g. a manual play-now request), the engine
        # can sit idle forever despite a perfectly good approved log
        # existing for the current hour. Manual mode is excluded: both
        # decks empty is the *intended* state of a manual hold (mic-only
        # talk-over after a song ends), not a stall -- without this
        # exclusion, this check fired every ~10s during a hold and
        # "recovered" by reloading the hour's log from position 0,
        # replaying its first item (almost always a Legal ID) on a loop
        # for as long as manual mode stayed on.
        with self._lock:
            idle = self.decks["A"] is None and self.decks["B"] is None
        if idle and not self.manual_mode:
            self._load_log_for(now.date(), now.hour)
            if self.log_items:
                self._start_next_track()

        seconds_left_in_hour = 3600 - (now.minute * 60 + now.second)
        if seconds_left_in_hour <= NEXT_HOUR_LOOKAHEAD_SECONDS:
            next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            self._ensure_log_approved(next_hour.date(), next_hour.hour)
            self._advance_to_next_hour_log(next_hour.date(), next_hour.hour)

        return True

    def _advance_to_next_hour_log(self, target_date, target_hour):
        """Broadcast-clock behavior: once the next hour's log is already
        approved (built above, ~NEXT_HOUR_LOOKAHEAD_SECONDS before top of
        hour), immediately swap the engine's active queue over to it —
        discarding whatever's left unplayed from the current hour — rather
        than waiting for the current hour's queue to drain naturally.
        Whatever's already playing on a deck is untouched and finishes
        normally; only what plays *next* (via the crossfade trigger or a
        natural EOS) changes. Idempotent — only swaps once per hour, since
        this runs on every 10s tick during the lookahead window."""
        if (
            self.current_log
            and self.current_log.date == target_date
            and self.current_log.hour == target_hour
        ):
            return  # already advanced

        close_old_connections()
        log = (
            PlaylistLog.objects
            .filter(date=target_date, hour=target_hour, status="approved")
            .first()
        )
        if not log:
            return
        items = list(
            log.items
            .select_related("track", "track__artist", "track__album", "track__category", "track__category__kind")
            .order_by("position")
        )
        if not items:
            return

        self.current_log = log
        self.log_items = items
        self._queue_cursor = 0
        self._next_hour_peek = None
        self._next_hour_peek_at = 0.0
        print(f"  Advanced active queue to next hour ahead of TOH: {log.date} {log.hour:02d}:00 ({len(items)} items)")

    def _ensure_log_approved(self, target_date, hour):
        """Build + approve target_date/hour's log if nothing exists yet
        for it. Returns True if a log was built just now."""
        if PlaylistLog.objects.filter(date=target_date, hour=hour).exists():
            return False

        log, error = build_hour_log(target_date, hour)
        if error:
            print(f"  Auto-build skipped for {target_date} {hour:02d}:00 — {error}")
            return False

        log.status = "approved"
        log.save(update_fields=["status"])
        print(f"  Auto-built and approved log for {target_date} {hour:02d}:00 ({log.items.count()} items)")
        return True

    # --- Slot / queue helpers ---

    def _other_slot(self, slot):
        return "B" if slot == "A" else "A"

    def _free_slot(self):
        """Whichever slot is currently empty, preferring 'A'. None if
        both are occupied."""
        for slot in SLOTS:
            if self.decks[slot] is None:
                return slot
        return None

    def _leading_deck(self):
        """The deck whose position the crossfade trigger should watch.
        Normally there's exactly one non-paused occupied slot; during a
        brief crossfade overlap there can be two, in which case either
        is a fine reference point since only one direction (finishing)
        matters here."""
        for slot in SLOTS:
            deck = self.decks[slot]
            if deck and not deck.paused:
                return deck
        return None

    def _peek_next_hour(self):
        """Read-only look at the next hour's already-approved log (built
        by `_ensure_upcoming_logs` ~NEXT_HOUR_LOOKAHEAD_SECONDS before top
        of hour), without switching engine state to it. Cached briefly
        since this can be polled every _poll_position tick (500ms) once
        the current hour's queue is running low. Mirrors the same
        `next_hour = (now + timedelta(hours=1)).replace(...)` pattern used
        elsewhere in this file, so day-rollover behaves identically."""
        now = time.time()
        if self._next_hour_peek is not None and now - self._next_hour_peek_at < 5.0:
            return self._next_hour_peek

        close_old_connections()
        wall_now = timezone.localtime()
        next_hour = (wall_now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        log = (
            PlaylistLog.objects
            .filter(date=next_hour.date(), hour=next_hour.hour, status="approved")
            .first()
        )
        result = None
        if log:
            items = list(
                log.items
                .select_related("track", "track__artist", "track__album", "track__category", "track__category__kind")
                .order_by("position")
            )
            if items:
                result = (log, items)

        self._next_hour_peek = result
        self._next_hour_peek_at = now
        return result

    def _extend_current_log_live(self):
        """Called when the queue is exhausted but the real next hour isn't
        built yet — early exhaustion, e.g. a DJ skipped/ejected a few
        tracks and burned through the hour's content faster than its
        nominal pacing assumed. Uses the same Log Fill Configuration
        strategy that pads a short log at build time (`fill_remaining_hour`)
        to append more tracks to the *live, already-approved* log instead
        of falling back to replaying it from the start (`_on_log_exhausted`'s
        `now.hour` reload)."""
        if not self.current_log or not self.log_items:
            return False

        close_old_connections()
        now = timezone.localtime()
        seconds_left = 3600 - (now.minute * 60 + now.second)
        if seconds_left <= DURATION_FIT_MARGIN:
            return False  # basically at the real boundary anyway

        # hour_start (not `now`) is the correct reference point here: it's
        # what `accumulated_seconds` below is measured relative to, so
        # `scheduled_time = hour_start + accumulated_seconds` comes out to
        # ~now for the first new pick. Recency exclusion still correctly
        # covers everything played so far this hour because `existing_picks`
        # (passed explicitly) already includes the whole hour's log, not
        # just what a DB cutoff query keyed off hour_start would catch.
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        existing_picks = [{"track": li.track, "category": li.category} for li in self.log_items]
        original_count = len(existing_picks)
        accumulated = 3600 - seconds_left
        # fill_remaining_hour appends onto `existing_picks` in place and
        # returns that same list, so the new items must be sliced off using
        # the count captured *before* the call.
        all_picks, _ = fill_remaining_hour(existing_picks, accumulated, hour_start)
        new_picks = all_picks[original_count:]
        if not new_picks:
            return False

        start_position = self.log_items[-1].position + 1
        new_items = append_fill_items(self.current_log, new_picks, start_position)
        self.log_items.extend(new_items)
        print(f"  Extended live log with {len(new_items)} fill track(s) — {seconds_left:.0f}s left in the real hour")
        return True

    def _roll_over_to_next_hour(self):
        """Called when the current hour's queue is exhausted. If the next
        hour's log has already been auto-built (normally true — it's
        approved NEXT_HOUR_LOOKAHEAD_SECONDS before top of hour), switch
        the engine's active log over to it so playback and the crossfade
        trigger both continue seamlessly across the boundary, instead of
        waiting for the natural-EOS `_on_log_exhausted` path to notice.
        If the real next hour isn't built yet — early exhaustion, before
        the actual top of hour — extend the live log instead of returning
        False (which would fall through to `_on_log_exhausted`'s
        replay-current-hour-from-scratch fallback)."""
        peek = self._peek_next_hour()
        if peek:
            log, items = peek
            self.current_log = log
            self.log_items = items
            self._queue_cursor = 0
            self._next_hour_peek = None
            self._next_hour_peek_at = 0.0
            print(f"  Rolled over to next hour's log: {log.date} {log.hour:02d}:00 ({len(items)} items)")
            return True
        return self._extend_current_log_live()

    def _try_extend_live_log(self):
        """Throttled wrapper around `_extend_current_log_live`, called from
        the `_poll_position` lookahead (every tick, ~500ms) once the
        current hour's queue is exhausted with no real next hour ready
        yet. Without this, the live-extend only ever ran from
        `_roll_over_to_next_hour` at the moment the last known track hit
        natural EOS — too late for the crossfade trigger (which needs
        `next_item` populated *before* `pos >= trigger_point`) to ever see
        the freshly re-picked track, so the handoff was always a hard cut.
        Calling this proactively, as soon as the gap is detected, gives the
        re-pick time to land before the countdown reaches its normal
        trigger point, so it can crossfade in like any other track.
        Throttled to once per 5s since a persistently-empty fallback
        category (e.g. `LogFillConfig` misconfigured) would otherwise retry
        on every single poll tick for however long the current track has
        left to play."""
        now = time.time()
        if now - self._last_live_extend_attempt < 5.0:
            return False
        self._last_live_extend_attempt = now
        return self._extend_current_log_live()

    def _next_queue_item(self):
        if self._queue_cursor >= len(self.log_items):
            if not self._roll_over_to_next_hour():
                return None
        item = self.log_items[self._queue_cursor]
        self._queue_cursor += 1
        return item

    def _get_upcoming_preview(self):
        """Every remaining item in the current hour's log — however many
        there are, no cap — plus, once those run out, the next hour's
        already-approved items — purely for UI preview (queue table /
        idle-deck 'Up Next'). Read-only: does not touch
        `self._queue_cursor` or `self.current_log`."""
        items = list(self.log_items[self._queue_cursor:])
        if not items:
            peek = self._peek_next_hour()
            if peek:
                items = list(peek[1])
        return items

    def _start_next_track(self, slot=None):
        """Load the next queued item into `slot` (or whichever slot is
        free, preferring A) and start it playing right away."""
        if slot is None:
            slot = self._free_slot()
        if slot is None:
            return

        log_item = self._next_queue_item()
        if log_item is None:
            self._on_log_exhausted(slot)
            return

        self._next_triggered = False
        deck = self._create_deck(slot, log_item)
        if deck is None:
            # File missing — move on to whatever's after it for this slot.
            self._start_next_track(slot=slot)
            return

        with self._lock:
            self.decks[slot] = deck
        self._deck_bin_map[id(deck.pipeline)] = deck

        bus = deck.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_deck_error, deck)

    def _apply_pad_offset(self, deck_bin, internal_position_ns=0):
        """A bin linked into the already-playing audiomixer needs its
        src pad's running-time offset corrected, or GStreamer treats
        its buffers as already old (by however long the pipeline's
        been running) and skips/drops through them to catch up —
        playback becomes audible partway into the track instead of at
        its start. `internal_position_ns` is wherever this bin's own
        timeline is starting from — 0 for a fresh track, or the frozen
        position when resuming a paused deck."""
        clock = self.main_pipeline.get_clock()
        if not clock:
            return
        running_time = clock.get_time() - self.main_pipeline.get_base_time()
        deck_bin.get_static_pad("src").set_offset(running_time - internal_position_ns)

    def _create_deck(self, slot, log_item, resume_position_ns=None):
        track = log_item.track
        filepath = track.filepath

        if not Path(filepath).is_file():
            print(f"  File not found: {filepath}")
            return None

        self._write_now_playing(track)

        src = Gst.ElementFactory.make("filesrc", None)
        src.set_property("location", filepath)

        decode = Gst.ElementFactory.make("decodebin", None)
        convert = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)

        bin_name = f"deck_{slot}_{log_item.id}_{int(time.time() * 1000)}"
        deck_bin = Gst.Bin.new(bin_name)
        deck_bin.add(src)
        deck_bin.add(decode)
        deck_bin.add(convert)
        deck_bin.add(resample)

        src.link(decode)
        convert.link(resample)

        ghost_pad = Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC)
        deck_bin.add_pad(ghost_pad)

        # Prime fresh track starts with a short burst of real silence
        # ahead of the decoded audio, via `concat` (plays sink_0 to EOS,
        # then seamlessly switches to sink_1 with adjacent timestamps).
        # audiotestsrc has zero decode latency, so this segment is ready
        # essentially instantly, giving decodebin's real file-open/demux/
        # decode/negotiate work the whole window to finish in the
        # background before the real audio is ever needed downstream.
        # The pad is never paused or idle — it produces real (if silent)
        # data continuously from the moment it's linked — so this
        # doesn't reproduce the aggregator-stall or sync-baseline issues
        # from two earlier attempts at this same bug.
        #
        # Redeployed after a stall on the first live test turned out to
        # most likely be caused by a *different*, pre-existing risk
        # (_seek_deck's flushing seek on a live-mixer-linked bin — see
        # its own docstring, a documented deadlock-adjacent issue from
        # earlier this session) used as a testing shortcut immediately
        # before the transition, not this mechanism itself. Redeploying
        # to test cleanly without that shortcut in the loop.
        #
        # Skipped for resumes/seeks (resume_position_ns set) — those
        # already explicitly seek to an arbitrary position via a
        # separate seek_simple() call right after creation, which would
        # need extra bookkeeping to land correctly across a
        # silence+real-content boundary, and this bug is specifically
        # about fresh starts on a normal crossfade.
        silence_primed = resume_position_ns is None
        if silence_primed:
            caps = Gst.Caps.from_string(f"audio/x-raw,rate={self.pipeline_sample_rate},channels=2")

            real_caps = Gst.ElementFactory.make("capsfilter", None)
            real_caps.set_property("caps", caps)

            silence = Gst.ElementFactory.make("audiotestsrc", None)
            silence.set_property("wave", "silence")
            silence.set_property("samplesperbuffer", int(SILENCE_PRIME_SECONDS * self.pipeline_sample_rate))
            silence.set_property("num-buffers", 1)
            silence_caps = Gst.ElementFactory.make("capsfilter", None)
            silence_caps.set_property("caps", caps)

            concat = Gst.ElementFactory.make("concat", None)

            for el in [real_caps, silence, silence_caps, concat]:
                deck_bin.add(el)

            resample.link(real_caps)
            silence.link(silence_caps)
            # Request silence's concat sink first so it gets sink_0
            # (concat plays request-numbered sinks in order).
            silence_caps.get_static_pad("src").link(concat.request_pad_simple("sink_%u"))
            real_caps.get_static_pad("src").link(concat.request_pad_simple("sink_%u"))

            # concat's src pad exists immediately (no dynamic negotiation
            # needed, unlike decodebin's), so the ghost pad's target can
            # be fixed right away instead of waiting for pad-added.
            ghost_pad.set_target(concat.get_static_pad("src"))

            def on_pad_added(element, pad):
                if pad.get_current_caps():
                    struct = pad.get_current_caps().get_structure(0)
                    if struct.get_name().startswith("audio"):
                        pad.link(convert.get_static_pad("sink"))
        else:
            def on_pad_added(element, pad):
                if pad.get_current_caps():
                    struct = pad.get_current_caps().get_structure(0)
                    if struct.get_name().startswith("audio"):
                        pad.link(convert.get_static_pad("sink"))
                        ghost_pad.set_target(resample.get_static_pad("src"))

        decode.connect("pad-added", on_pad_added)

        # Block EOS from reaching audiomixer — we handle track
        # completion via position polling instead
        def eos_probe(pad, info):
            event = info.get_event()
            if event.type == Gst.EventType.EOS:
                GLib.idle_add(self._on_deck_eos_probed, deck_bin)
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        ghost_pad.add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM,
            eos_probe,
        )

        self.main_pipeline.add(deck_bin)

        mixer_pad = self.mixer.request_pad_simple("sink_%u")
        deck_bin.get_static_pad("src").link(mixer_pad)

        self._apply_pad_offset(deck_bin, internal_position_ns=resume_position_ns or 0)

        deck_bin.sync_state_with_parent()

        deck = Deck(
            slot=slot,
            track=track,
            log_item=log_item,
            pipeline=deck_bin,
            mixer_pad=mixer_pad,
            silence_primed=silence_primed,
        )
        start_offset = (resume_position_ns or 0) / Gst.SECOND
        # For primed decks, "position 0" (start of the real content) is
        # SILENCE_PRIME_SECONDS after creation, not immediately — shift
        # started_at forward to match, so the wall-clock position
        # estimate (_get_deck_position) lines up with the real track's
        # own timeline (what next_start_seconds/duration_seconds are
        # computed against), not the silence-inclusive elapsed time.
        silence_shift = SILENCE_PRIME_SECONDS if silence_primed else 0.0
        deck.started_at = time.time() - start_offset + silence_shift

        if resume_position_ns is None:
            try:
                close_old_connections()
                log_item.played_at = timezone.now()
                log_item.save(update_fields=["played_at"])
                Track.objects.filter(id=track.id).update(
                    last_played_at=timezone.now(),
                    play_count=track.play_count + 1,
                )
            except Exception as exc:
                print(f"  DB write failed (non-fatal): {exc}")
            print(f"  [{slot}] Playing: {track.artist.name if track.artist else '?'} - {track.title}")
        else:
            print(f"  [{slot}] Resumed: {track.artist.name if track.artist else '?'} - {track.title} at {start_offset:.1f}s")

        return deck

    def _warm_track_cache(self, log_item):
        """Decode the upcoming track once in a fully separate, throwaway
        Gst.Pipeline with zero connection to self.main_pipeline/the live
        mixer — warms the OS page cache and forces any one-time codec/
        plugin lookup ahead of time, so the real _create_deck (still the
        normal synchronous path, unchanged) should decode faster the
        second time, narrowing (not guaranteeing eliminating) the
        decode-latency window that clips the start of short tracks.

        Deliberately does not touch the live mixer at all — two prior
        attempts at fixing this by manipulating the live deck/pad
        directly (a first-buffer offset probe, then a true preroll
        linked into the live mixer) each broke something differently;
        this sidesteps that whole risk category by never linking
        anything into the shared pipeline.

        Idempotent — called every _poll_position tick once eligible, but
        only does anything the first time per track."""
        if self._cache_warm_item_id == log_item.id:
            return

        filepath = log_item.track.filepath
        if not Path(filepath).is_file():
            return
        self._cache_warm_item_id = log_item.id

        warm_pipeline = Gst.Pipeline.new("cache-warmer")
        src = Gst.ElementFactory.make("filesrc", None)
        src.set_property("location", filepath)
        decode = Gst.ElementFactory.make("decodebin", None)
        convert = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)
        sink = Gst.ElementFactory.make("fakesink", None)

        for el in [src, decode, convert, resample, sink]:
            warm_pipeline.add(el)

        src.link(decode)
        convert.link(resample)
        resample.link(sink)

        def on_pad_added(element, pad):
            if pad.get_current_caps():
                struct = pad.get_current_caps().get_structure(0)
                if struct.get_name().startswith("audio"):
                    pad.link(convert.get_static_pad("sink"))

        decode.connect("pad-added", on_pad_added)

        warm_pipeline.set_state(Gst.State.PLAYING)
        GLib.timeout_add_seconds(2, self._teardown_cache_warmer, warm_pipeline)

    def _teardown_cache_warmer(self, warm_pipeline):
        warm_pipeline.set_state(Gst.State.NULL)
        return False  # one-shot GLib timeout, don't repeat

    def _remove_deck(self, deck):
        with self._lock:
            self._deck_bin_map.pop(id(deck.pipeline), None)
            deck.pipeline.set_state(Gst.State.NULL)
            try:
                deck.pipeline.get_static_pad("src").unlink(deck.mixer_pad)
            except Exception:
                pass
            if deck.mixer_pad is not None:
                self.mixer.release_request_pad(deck.mixer_pad)
            self.main_pipeline.remove(deck.pipeline)
            deck.finished = True
            if self.decks.get(deck.slot) is deck:
                self.decks[deck.slot] = None

    def _get_deck_position(self, deck):
        if deck.paused:
            return deck.paused_position
        if deck.silence_primed:
            # query_position() isn't reliable through concat's internal
            # segment-switch (silence -> real content) — it can report a
            # frozen/stale value instead of failing cleanly, so the
            # query-then-fallback pattern below doesn't catch it. Wall
            # clock is accurate enough for our purposes and sidesteps it
            # entirely. started_at is already shifted by
            # SILENCE_PRIME_SECONDS at creation time (_create_deck), so
            # this lines up with the real track's own timeline.
            if deck.started_at:
                return max(0.0, time.time() - deck.started_at)
            return 0.0
        ok, position = deck.pipeline.query_position(Gst.Format.TIME)
        if ok:
            return position / Gst.SECOND
        if deck.started_at:
            return time.time() - deck.started_at
        return 0.0

    def _check_commands(self):
        try:
            if not CMD_PATH.is_file():
                return
            data = json.loads(CMD_PATH.read_text(encoding="utf-8"))
            CMD_PATH.unlink(missing_ok=True)

            cmd = data.get("command")
            if cmd == "seek":
                position = float(data.get("position", 0))
                slot = data.get("slot")
                self._seek_deck(slot, position)
            elif cmd == "reload_audio_output":
                self._apply_audio_output_device(self._resolve_studio_monitor_device())
            elif cmd == "reload_agc_config":
                self._apply_agc_config()
            elif cmd == "reload_current_log":
                self._reload_and_restart_current_log()
            elif cmd == "deck_pause":
                self._pause_deck(data.get("slot"))
            elif cmd == "deck_resume":
                self._resume_deck(data.get("slot"))
            elif cmd == "deck_eject":
                self._eject_deck(data.get("slot"))
            elif cmd == "force_next_hour":
                # Manual testing hook: do exactly what
                # _ensure_upcoming_logs does naturally ~30s before top of
                # hour, on demand instead of waiting for the real clock.
                now = timezone.localtime()
                next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                self._ensure_log_approved(next_hour.date(), next_hour.hour)
                self._advance_to_next_hour_log(next_hour.date(), next_hour.hour)
            elif cmd == "mic_ptt":
                self._set_mic_ptt(bool(data.get("active")))
            elif cmd == "set_manual_mode":
                self._set_manual_mode(bool(data.get("active")))
            elif cmd == "insert_urgent":
                self._insert_urgent_next(data.get("category", "WxAlert"))
            elif cmd == "remote_dj_gate":
                self._remote_dj_set_gate(bool(data.get("active")))
            elif cmd == "remote_dj_disconnect":
                self._remote_dj_session_stop()
        except Exception as exc:
            print(f"  Command error: {exc}")

    def _insert_urgent_next(self, category_code):
        """Splice a track from `category_code` into the live queue at the
        current cursor position, so it plays at the very next track
        boundary — the same "load next track" path every normal
        crossfade already goes through (_next_queue_item ->
        _start_next_track). Deliberately does NOT touch a
        currently-playing deck's live GStreamer state — a true mid-track
        preempt carries real risk (see the seek-near-crossfade caveat
        elsewhere in this file); this instead reuses the same
        live-log-mutation pattern _extend_current_log_live already uses
        safely, just inserting at the front of the queue instead of the
        back.

        category_code is expected to resolve to exactly one ready2air
        track (e.g. WxAlert) — same "one file, always fresh on disk"
        pattern as WxTemp/WxForecast/WxObs, delivered via
        lib/delivery.py's sync_track_file call before this command is
        ever fired."""
        if not self.current_log or not self.log_items:
            print("  insert_urgent: no live log loaded — ignoring")
            return

        close_old_connections()
        category = Category.objects.filter(code=category_code).first()
        if category is None:
            print(f"  insert_urgent: unknown category {category_code!r}")
            return
        track = (
            Track.objects.filter(category=category, ready2air=True)
            .select_related("artist")
            .first()
        )
        if track is None:
            print(f"  insert_urgent: no ready2air track in category {category_code!r}")
            return

        insert_at = self._queue_cursor
        now = timezone.localtime()

        if insert_at < len(self.log_items):
            # Shift every not-yet-played item's position up by 1 — in the
            # DB and in-memory — so the new item sorts correctly on any
            # future DB re-query without touching anything already played.
            # Two-phase (through a disjoint offset range, then back down)
            # because LogItem has a unique_together=("playlist_log",
            # "position") constraint that Postgres checks per-row as a
            # single ascending "+1" UPDATE executes — row N's shift to
            # N+1 collides with row N+1's still-unshifted value before
            # row N+1 gets its own turn. The offset makes every
            # intermediate value disjoint from any remaining original one.
            insert_position = self.log_items[insert_at].position
            qs = LogItem.objects.filter(playlist_log=self.current_log, position__gte=insert_position)
            qs.update(position=F("position") + 100000)
            LogItem.objects.filter(
                playlist_log=self.current_log, position__gte=insert_position + 100000,
            ).update(position=F("position") - 99999)
            for item in self.log_items[insert_at:]:
                item.position += 1
            new_position = insert_position
        else:
            new_position = (self.log_items[-1].position + 1) if self.log_items else 0

        new_item = LogItem.objects.create(
            playlist_log=self.current_log,
            position=new_position,
            scheduled_time=now,
            track=track,
            track_title=track.title,
            track_artist=track.artist.name if track.artist_id else "",
            category=category,
        )
        self.log_items.insert(insert_at, new_item)
        print(f"  Inserted urgent track ({category_code}) at queue position {insert_at}: {track.title}")

    def _apply_talk_ducking(self):
        """Shared by local mic PTT and the remote-DJ gate -- both are
        "someone is talking" events from the same listener's perspective,
        so both duck the decks the same way. Ducks if EITHER is live --
        both gate state fields are read fresh here rather than trusting
        the caller's `active` arg, so a mic toggling off while the OTHER
        is still live doesn't spuriously un-duck the decks under the
        talker who's still going (real bug found live during Stage 6
        testing: an operator asking "what if I gate the local PTT on
        while the remote is also live?" would have hit exactly this on
        the follow-up gate-off).

        Ducking config is read fresh from the DB on every toggle rather
        than needing its own live-reload command/signal -- unlike AGC
        (which must reflect changes to a continuously-running effect),
        ducking's configured values only ever matter at the instant of a
        PTT-style transition, so there's nothing to keep in sync between
        saves."""
        cfg = DuckingConfig.load()
        remote_live = bool(
            self.remote_dj_session and self.remote_dj_session.remote_gate
            and self.remote_dj_session.remote_gate.get_property("volume") > 0.0
        )
        any_live = self.mic_live or remote_live
        target = (10 ** (cfg.duck_level_db / 20.0)) if (cfg.enabled and any_live) else 1.0
        self._start_duck_ramp(target)

    def _set_mic_ptt(self, active):
        if self.mic_ptt_volume is None:
            print("  mic_ptt requested but mic is not configured/available — ignoring")
            return
        self.mic_ptt_volume.set_property("volume", 1.0 if active else 0.0)
        self.mic_live = active
        self._apply_talk_ducking()
        self._apply_mic_mode_hold()
        print(f"  Mic PTT: {'ON' if active else 'OFF'}")

    def _any_mic_live(self):
        return self.mic_live or bool(
            self.remote_dj_session and self.remote_dj_session.remote_gate
            and self.remote_dj_session.remote_gate.get_property("volume") > 0.0
        )

    def _apply_mic_mode_hold(self):
        """Called after any mic state change (local PTT or remote gate).
        Ties Manual mode to "any mic live" -- matches how the operator
        actually uses this in practice: they toggle the mic on around a
        track transition, expecting the current track to keep playing
        while they talk over the tail and the next track to wait until
        they finish. Auto handles the middle-of-song voiceover case
        naturally: mic goes on for a few seconds, mic goes off before
        the next trigger, mode flips back to Auto in time for the
        automatic handoff.

        Semantics:
        - Mic goes live in Auto mode -> switch to Manual, remember we
          did that (`_manual_from_mic = True`).
        - Mic goes live in Manual mode (operator already chose it) -> no
          change; `_manual_from_mic` stays False so the manually-chosen
          Manual survives the next mic release.
        - Last mic goes quiet with `_manual_from_mic = True` -> restore
          Auto.
        - Last mic goes quiet with `_manual_from_mic = False` (operator
          set Manual explicitly before/during the mic) -> stay in
          Manual.

        An explicit operator toggle of manual_mode CLEARS
        `_manual_from_mic` (see `_set_manual_mode`), so a user override
        during a live mic wins and doesn't get overwritten on the next
        mic transition.
        """
        any_live = self._any_mic_live()
        if any_live and not self.manual_mode:
            self.manual_mode = True
            self._manual_from_mic = True
            print("  Manual mode: ON (auto -- held by mic)")
        elif not any_live and self._manual_from_mic:
            # Reuse _set_manual_mode(False) so a genuinely-due handoff
            # that was withheld during the mic hold fires immediately.
            self._set_manual_mode(False, _from_mic_release=True)

    def _set_manual_mode(self, active, _from_mic_release=False):
        """`_from_mic_release=True` is the internal path used by
        `_apply_mic_mode_hold` to restore Auto once the last mic goes
        quiet -- it suppresses the "operator override" flag clear that
        an operator-initiated toggle would apply. All external command
        dispatch (see _check_commands) uses the default form."""
        self.manual_mode = active
        if not _from_mic_release:
            # Explicit operator toggle takes ownership -- any later mic
            # transition should NOT overwrite this.
            self._manual_from_mic = False
        reason = " (auto -- mic released)" if _from_mic_release else ""
        print(f"  Manual mode: {'ON' if active else 'OFF'}{reason}")
        if not active and self._manual_hold_pending:
            # A handoff was genuinely due (trigger point reached, or the
            # leading deck fully finished) and only manual mode withheld
            # it -- start the next track right now instead of waiting on
            # _poll_position, which may have nothing left to poll if the
            # leading deck already finished during the hold.
            self._manual_hold_pending = False
            self._next_triggered = False
            self._start_next_track()

    # ------------------------------------------------------------------
    # Remote DJ over WebRTC (see /home/jreed/.claude/plans/warm-zooming-rose.md)
    # ------------------------------------------------------------------
    def _warm_stun_dns(self):
        """One-shot DNS resolve of the configured STUN host at engine
        start. Live remote-DJ connects from cellular were exposing a
        cold-DNS-cache first-connect stall: a fresh resolve of e.g.
        stun.l.google.com could add hundreds of ms during pipeline
        bring-up, and that stretched the ICE window enough to surface
        the static-on-playing-deck race documented in
        _remote_dj_build_session. Blocking on purpose (a broken STUN
        DNS at boot is a real diagnostic signal); failure here is not
        fatal, a real session start would just do its own resolve
        anyway."""
        url = (RemoteDJConfig.load().stun_server or "").strip()
        if "://" in url:
            url = url.split("://", 1)[1]
        host = url.split(":", 1)[0].split("/", 1)[0]
        if not host:
            return
        try:
            addr = socket.gethostbyname(host)
            print(f"  Remote DJ: warmed STUN DNS for {host} -> {addr}")
        except Exception as exc:
            print(f"  Remote DJ: STUN DNS warm-up failed for {host}: {exc}")

    def _remote_dj_pipeline_caps(self):
        """concat requires its sink pads to share genuinely IDENTICAL
        caps, not just a matching rate -- validated the hard way in the
        offline harness (silence defaulting to mono/S16LE vs. the decoded
        mic path defaulting to stereo/F32LE deadlocked negotiation with
        no bus error). One fully-specified caps string, used to fixate
        both of this session's own concat feeds (silence + decoded real
        audio) -- self-contained to this concat, independent of whatever
        format master_mixer itself ultimately negotiates downstream."""
        return Gst.Caps.from_string(
            f"audio/x-raw,format=S16LE,rate={self.pipeline_sample_rate},"
            f"channels=2,layout=interleaved",
        )

    def _remote_dj_session_start(self):
        if self.remote_dj_session is not None:
            print("  Remote DJ session start requested but one is already active — ignoring")
            return False
        if self.remote_dj_tee is None:
            print("  Remote DJ session start requested but the feature isn't built (RemoteDJConfig.enabled was off at pipeline build time) — ignoring")
            return False

        print("  Remote DJ: session starting")
        session = RemoteDJSession()
        self.remote_dj_session = session
        try:
            self._remote_dj_build_session(session)
        except Exception as exc:
            # A failure partway through must not leave
            # self.remote_dj_session stuck non-None -- that would
            # silently block every future session until the next engine
            # restart. Found the hard way: a wrong GStreamer property
            # name here left exactly this stuck state during Stage 5's
            # own first live test, recovered only because GLib.idle_add
            # already isolates exceptions from crashing the main loop.
            print(f"  Remote DJ session start failed, rolling back: {exc}")
            self.remote_dj_session = None
            for el in session.elements:
                el.set_state(Gst.State.NULL)
                if el.get_parent() is self.main_pipeline:
                    self.main_pipeline.remove(el)
            if session.master_mixer_pad is not None:
                self.master_mixer.release_request_pad(session.master_mixer_pad)
            if session.monitor_tee_pad is not None:
                self.remote_dj_tee.release_request_pad(session.monitor_tee_pad)
            if self._remote_dj_server:
                self._remote_dj_server.disconnect_threadsafe()
        return False

    def _remote_dj_build_session(self, session):
        cfg = RemoteDJConfig.load()

        session.webrtc = Gst.ElementFactory.make("webrtcbin", None)
        session.webrtc.set_property("stun-server", cfg.stun_server)
        session.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        # rtpbin latency (the internal jitterbuffer's target buffered
        # depth). Default is 200ms. Lower is fine for LAN-ish paths and
        # keeps the observed inbound-mic-path latency capped -- setting
        # this high is one path by which jitterbuffer content can
        # accumulate. 40ms is aggressive but reasonable for the
        # low-latency talk-over use case; if we ever see packet-loss
        # dropouts in real-world use, raise this.
        session.webrtc.set_property("latency", 40)
        # The port-range properties live on webrtcbin's ice-agent
        # sub-object (a GstWebRTCNice), not directly on webrtcbin itself
        # -- confirmed by introspecting a real instance's properties
        # (webrtcbin only exposes stun-server/ice-transport-policy/etc.
        # directly; min-rtp-port/max-rtp-port are on ice-agent).
        #
        # MUST keep a persistent reference (session.ice_agent, not a bare
        # local) -- this is what caused the real SIGSEGV during Stage 5's
        # first live test. get_property("ice-agent") hands back a PyGObject
        # wrapper that does NOT hold webrtcbin's own internal reference
        # alive on its own; letting the local variable go out of scope at
        # the end of this method lets Python's GC finalize the underlying
        # GstWebRTCICE C object while webrtcbin's internal negotiation code
        # still expects to use it later. That produced the exact
        # `assertion 'GST_IS_WEBRTC_ICE (ice)' failed` /
        # `GST_IS_WEBRTC_ICE_TRANSPORT` critical warnings immediately
        # before the crash, and was reproduced/confirmed offline in
        # isolation (no Django, no production pipeline) with a bare
        # two-webrtcbin loopback: real negotiation always segfaults with
        # only a local reference, and always completes cleanly once the
        # reference is kept alive for the session's lifetime.
        session.ice_agent = session.webrtc.get_property("ice-agent")
        session.ice_agent.set_property("min-rtp-port", cfg.ice_udp_min_port)
        session.ice_agent.set_property("max-rtp-port", cfg.ice_udp_max_port)
        self.main_pipeline.add(session.webrtc)
        session.elements.append(session.webrtc)

        # --- Outbound: monitor-return branch. Mix-minus by construction
        # for the *remote* mic (never tapped here), but INCLUDES the
        # ducked deck audio and the local studio mic post-PTT so the
        # remote DJ can converse with the studio operator. A small
        # per-session audiomixer at the head of this branch sums the
        # two inputs:
        #   - remote_dj_tee.src_%u -> ducked decks (as always)
        #   - local_mic_tee.src_%u -> local mic post mic_ptt_volume,
        #     so the mic is silent-flowing when the operator's PTT is
        #     off (the mic_ptt_volume element multiplies by 0.0/1.0
        #     rather than dropping buffers, per the same lesson that
        #     drove `mic_ptt_valve -> mic_ptt_volume` on the main
        #     mixer -- keeps audiomixer's per-pad-first-buffer
        #     requirement satisfied without an operator toggle).
        # local_mic_tee is only built when both remote_dj is enabled
        # AND local mic exists, so the mic branch here only fires
        # under the same condition.
        mon_mixer = Gst.ElementFactory.make("audiomixer", None)
        # Per-input decouple queues between the always-flowing tees and
        # this session's monitor mixer. LEAKY is load-bearing here, not
        # an optimization: until webrtcbin's ICE/DTLS negotiation
        # completes (1-3s, longer on mobile networks), NOTHING downstream
        # of these queues consumes audio. A non-leaky queue fills its
        # default ~1s cap and then BLOCKS the tee feeding it -- and a
        # blocked tee stalls ALL its branches, including remote_dj_tee's
        # on-air branch into master_mixer. Observed live as a ~2s
        # full-output dropout on every remote-DJ connect. Same lesson,
        # same fix as stereotool_queue: this branch may only ever drop
        # its own audio, never block the shared path.
        mon_decks_q = Gst.ElementFactory.make("queue", None)
        mon_decks_q.set_property("leaky", 2)  # downstream -- drop oldest, never block the tee
        mon_decks_q.set_property("max-size-time", REMOTE_DJ_MONITOR_QUEUE_MS * Gst.MSECOND)
        mon_decks_q.set_property("max-size-buffers", 0)
        mon_decks_q.set_property("max-size-bytes", 0)
        mon_mic_q = None
        if self.local_mic_tee is not None:
            mon_mic_q = Gst.ElementFactory.make("queue", None)
            mon_mic_q.set_property("leaky", 2)
            mon_mic_q.set_property("max-size-time", REMOTE_DJ_MONITOR_QUEUE_MS * Gst.MSECOND)
            mon_mic_q.set_property("max-size-buffers", 0)
            mon_mic_q.set_property("max-size-bytes", 0)
        mon_q = Gst.ElementFactory.make("queue", None)
        mon_q.set_property("leaky", 1)  # upstream -- can only ever drop its own data
        mon_q.set_property("max-size-time", REMOTE_DJ_MONITOR_QUEUE_MS * Gst.MSECOND)
        mon_conv = Gst.ElementFactory.make("audioconvert", None)
        mon_resample = Gst.ElementFactory.make("audioresample", None)
        mon_caps = Gst.ElementFactory.make("capsfilter", None)
        # `channels=2` is load-bearing here, not cosmetic: without it,
        # every element in the mon_conv -> mon_resample -> mon_caps ->
        # mon_enc -> mon_pay chain is pass-through channel-count-wise
        # and negotiation reverse-flows from webrtcbin's SDP offer. And
        # for the SENDONLY transceiver (unlike the RECVONLY one below
        # which pins `encoding-params=(string)2` explicitly) we don't
        # set any caps -- so the SDP offer goes out with no `stereo=1`
        # in the Opus fmtp, Chrome answers mono per RFC 7587's default,
        # the whole chain reverse-negotiates to mono, and audioconvert
        # cheerfully downmixes the stereo studio mix before opusenc
        # ever sees it. Pinning stereo here at the top of the Opus
        # chain propagates forward: opusenc encodes stereo Opus,
        # rtpopuspay's src caps carry `encoding-params=(string)2`,
        # webrtcbin's SDP offer includes `stereo=1;sprop-stereo=1`,
        # Chrome accepts, and the remote DJ hears an actual stereo
        # image instead of a mono downmix. ~40 kbps -> ~80 kbps on
        # the outbound Opus stream, trivial on both LAN and cellular.
        mon_caps.set_property(
            "caps",
            Gst.Caps.from_string(f"audio/x-raw,rate={REMOTE_DJ_OPUS_RATE},channels=2"),
        )
        mon_enc = Gst.ElementFactory.make("opusenc", None)
        mon_enc.set_property("frame-size", REMOTE_DJ_OPUS_FRAME_SIZE_MS)
        mon_pay = Gst.ElementFactory.make("rtpopuspay", None)
        mon_elements = [mon_mixer, mon_decks_q, mon_q, mon_conv, mon_resample, mon_caps, mon_enc, mon_pay]
        if mon_mic_q is not None:
            mon_elements.append(mon_mic_q)
        for el in mon_elements:
            self.main_pipeline.add(el)
            session.elements.append(el)

        # Internal monitor-chain links only. The tee pads that feed this
        # chain are deliberately NOT requested or linked here -- that
        # happens as the VERY LAST step of session build (see the bottom
        # of this method), after every session element including
        # webrtcbin has been synced to PLAYING. Linking a live tee into
        # a chain with any still-NULL element downstream produces flow
        # errors that propagate back through the tee into the shared
        # on-air path -- observed live as static on the currently-
        # playing deck that persisted until the deck's track ended.
        mon_mixer.link(mon_q)
        mon_q.link(mon_conv)
        mon_conv.link(mon_resample)
        mon_resample.link(mon_caps)
        mon_caps.link(mon_enc)
        mon_enc.link(mon_pay)

        # Transceiver order matters -- validated the hard way:
        # request_pad_simple("sink_%u") silently reuses any existing
        # pad-less transceiver of matching kind rather than creating a
        # new one. Requesting the send pad FIRST consumes a transceiver
        # immediately, so the later add-transceiver(RECVONLY) call below
        # is forced to create a genuinely separate one instead of
        # hijacking this one and collapsing both SDP m-lines into one.
        send_pad = session.webrtc.request_pad_simple("sink_%u")
        mon_pay.get_static_pad("src").link(send_pad)
        send_trans = send_pad.get_property("transceiver")
        send_trans.props.direction = GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY

        # A RECVONLY transceiver also needs explicit, fully-shaped Opus
        # RTP caps (encoding-params for channel count, not just
        # encoding-name/clock-rate/payload) -- without it, Chrome answers
        # with port=0 and a dummy PCMU codec, silently rejecting the line.
        recv_caps = Gst.Caps.from_string(
            f"application/x-rtp,media=audio,encoding-name=OPUS,"
            f"clock-rate={REMOTE_DJ_OPUS_RATE},encoding-params=(string)2,payload=97",
        )
        session.webrtc.emit("add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY, recv_caps)

        # --- Inbound: DEFERRED to _remote_dj_on_pad_added. ---
        # Earlier design used a silence-primed `concat` (see Stage 1 in
        # the plan) to keep master_mixer's newly-requested sink pad from
        # starving during ICE/DTLS negotiation. That worked for the first
        # session in an engine process but the second session in the same
        # process consistently went silent after ~10 buffers -- concat's
        # sink_0 EOS + still-flowing silence source + newly-active sink_1
        # got the internal state stuck, confirmed empirically via pad-
        # probe buffer-count instrumentation ("all three probes at
        # concat.src / gate.src / mixer.sink got exactly N=10 buffers in
        # session #2, ZERO after; upstream decode chain kept flowing").
        # Simpler approach that also removes the whole "which timestamps
        # is concat generating for the silence->real handover" problem:
        # don't request the master_mixer sink pad until the mic RTP feed
        # is actually decodable, i.e., after webrtcbin.pad-added has
        # fired. No pad → no starvation. Real audio arrives, we request
        # the pad and immediately have real content to feed it. The
        # tradeoff: master_mixer sees a new sink pad joining "hot" (with
        # data flowing from instant 0), but Stage 1's hot-add offline
        # harness already validated this operation as glitch-free.
        session.webrtc.connect("on-negotiation-needed", self._remote_dj_on_negotiation_needed)
        session.webrtc.connect("on-ice-candidate", self._remote_dj_on_ice_candidate)
        session.webrtc.connect("pad-added", self._remote_dj_on_pad_added)
        session.webrtc.connect("notify::connection-state", self._remote_dj_on_connection_state)

        # Fix ladder [1] + [2] from
        # remote-dj-cellular-static-fix-ladder.md: sync the fast
        # elements first, webrtcbin last, and wait for each to actually
        # reach PLAYING (up to 500 ms per element) before we proceed to
        # the tee link below.
        #
        # The previous fire-and-forget `sync_state_with_parent()`
        # returned control immediately, so under a stretched
        # webrtcbin bring-up window (cellular first-connect, cold STUN
        # DNS, etc.) a queue or resampler in this chain could still be
        # in PAUSED at tee-link time -- and linking a live tee into a
        # chain with any still-non-PLAYING element downstream produces
        # a flow error that propagates back through the shared tee onto
        # the on-air path (mechanism #1 in the ladder doc: observed
        # live as static on the currently-playing deck that persisted
        # until the deck's track ended and a new deck bin replaced it).
        # Ordering webrtcbin last means every light element is
        # definitely PLAYING before we wait on the heavy one.
        #
        # 500 ms is a soft ceiling: webrtcbin's own GStreamer state
        # change is fast (its ICE/DTLS work happens asynchronously
        # AFTER state=PLAYING, not gating it), so this timeout should
        # never actually trigger in practice -- but if some transient
        # bind/resolve delay does stretch a transition past half a
        # second, we raise here and let _remote_dj_session_start's
        # existing try/except roll the whole session back rather than
        # link a not-yet-live chain into the shared tee and glitch
        # the current deck. `.value_nick` on the enums renders the
        # short human name ("success"/"async"/"playing"/etc.) so the
        # rollback message is readable in the journal.
        fast_elements = [el for el in session.elements if el is not session.webrtc]
        sync_order = fast_elements + [session.webrtc]
        for el in sync_order:
            el.sync_state_with_parent()
            result, state, pending = el.get_state(500 * Gst.MSECOND)
            if result != Gst.StateChangeReturn.SUCCESS or state != Gst.State.PLAYING:
                raise RuntimeError(
                    f"{el.get_name()} did not reach PLAYING within 500 ms "
                    f"(result={result.value_nick} state={state.value_nick} "
                    f"pending={pending.value_nick}); aborting session build "
                    f"to protect on-air path"
                )

        # LAST step, deliberately after everything above is PLAYING:
        # splice the live tees into the (now fully-live, leaky-buffered)
        # monitor chain. From the instant these links land, the tees
        # push into queues that are guaranteed to never block and whose
        # downstream is guaranteed to never be NULL -- the two failure
        # modes that previously reached back through the shared tees
        # and glitched the on-air output (a ~2s dropout on connect, and
        # earlier, static on the playing deck).
        session.monitor_tee_pad = self.remote_dj_tee.request_pad_simple("src_%u")
        session.monitor_tee_pad.link(mon_decks_q.get_static_pad("sink"))
        mon_mixer_decks_sink = mon_mixer.request_pad_simple("sink_%u")
        mon_decks_q.get_static_pad("src").link(mon_mixer_decks_sink)

        if self.local_mic_tee is not None:
            session.local_mic_tee_pad = self.local_mic_tee.request_pad_simple("src_%u")
            session.local_mic_tee_pad.link(mon_mic_q.get_static_pad("sink"))
            mon_mixer_mic_sink = mon_mixer.request_pad_simple("sink_%u")
            mon_mic_q.get_static_pad("src").link(mon_mixer_mic_sink)

    def _remote_dj_on_negotiation_needed(self, element):
        session = self.remote_dj_session
        if session is None or session.webrtc is not element:
            return
        promise = Gst.Promise.new_with_change_func(self._remote_dj_on_offer_created, None)
        element.emit("create-offer", None, promise)

    def _remote_dj_on_offer_created(self, promise, _udata):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        # transfer-full gotcha, validated the hard way: set-local-
        # description takes ownership of the offer's boxed SDP, so
        # PyGObject nulls out offer.sdp once the signal consumes it --
        # extract the text BEFORE the emit call below, thread the string
        # (not the description object) through to the callback.
        sdp_text = offer.sdp.as_text()
        session = self.remote_dj_session
        if session is None:
            return
        promise2 = Gst.Promise.new_with_change_func(self._remote_dj_on_local_desc_set, sdp_text)
        session.webrtc.emit("set-local-description", offer, promise2)

    def _remote_dj_on_local_desc_set(self, promise, sdp_text):
        promise.wait()
        if self._remote_dj_server:
            self._remote_dj_server.send_json_threadsafe({"type": "offer", "sdp": sdp_text})

    def _remote_dj_on_ice_candidate(self, element, mline_index, candidate):
        session = self.remote_dj_session
        if session is None or session.webrtc is not element:
            return
        if self._remote_dj_server:
            self._remote_dj_server.send_json_threadsafe(
                {"type": "ice", "sdpMLineIndex": mline_index, "candidate": candidate},
            )

    def _remote_dj_handle_answer(self, sdp_text):
        session = self.remote_dj_session
        if session is None or sdp_text is None:
            return False
        res, sdpmsg = GstSdp.SDPMessage.new_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        promise = Gst.Promise.new()
        session.webrtc.emit("set-remote-description", answer, promise)
        promise.interrupt()
        return False

    def _remote_dj_handle_ice(self, mline_index, candidate):
        session = self.remote_dj_session
        if session is None or mline_index is None or candidate is None:
            return False
        session.webrtc.emit("add-ice-candidate", mline_index, candidate)
        return False

    def _remote_dj_on_pad_added(self, element, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return
        session = self.remote_dj_session
        if session is None or session.webrtc is not element:
            return

        depay = Gst.ElementFactory.make("rtpopusdepay", None)
        dec = Gst.ElementFactory.make("opusdec", None)
        conv = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)
        capsfilter = Gst.ElementFactory.make("capsfilter", None)
        # opusdec's own output is always 48kHz (Opus is 48kHz-native) --
        # this resamples DOWN to the pipeline's own fixated rate, not up.
        # Getting this backwards is exactly the sample-rate-mismatch
        # failure mode the offline harness flagged.
        capsfilter.set_property("caps", self._remote_dj_pipeline_caps())
        # Small queue to decouple the decode chain's thread from anything
        # downstream. Kept in place after the concat removal since the
        # gate volume element downstream doesn't have its own thread
        # either, and any downstream stall (e.g. transient master_mixer
        # pad-add negotiation) should not back up into webrtcbin's
        # internal rtpjitterbuffer thread.
        queue = Gst.ElementFactory.make("queue", None)
        remote_gain = Gst.ElementFactory.make("volume", None)
        # Software gain to compensate for browser WebRTC's typical output
        # being noticeably quieter than the local mic through the same
        # preamp -- see hardware.RemoteDJAudioInput. Read fresh here at
        # session start; changes take effect on the next connect (same
        # contract as AudioInput.gain_db for the local mic).
        remote_gain.set_property(
            "volume", 10 ** (RemoteDJAudioInput.load().gain_db / 20.0)
        )
        session.remote_gate = Gst.ElementFactory.make("volume", None)
        session.remote_gate.set_property("volume", 0.0)  # closed -- opened via remote_dj_gate command
        gate_conv = Gst.ElementFactory.make("audioconvert", None)
        for el in (depay, dec, conv, resample, capsfilter, queue,
                   remote_gain, session.remote_gate, gate_conv):
            self.main_pipeline.add(el)
            session.elements.append(el)
        depay.link(dec)
        dec.link(conv)
        conv.link(resample)
        resample.link(capsfilter)
        capsfilter.link(queue)
        queue.link(remote_gain)
        remote_gain.link(session.remote_gate)
        session.remote_gate.link(gate_conv)
        pad.link(depay.get_static_pad("sink"))

        for el in (depay, dec, conv, resample, capsfilter, queue,
                   remote_gain, session.remote_gate, gate_conv):
            el.sync_state_with_parent()

        # Master_mixer's sink pad is requested and linked ONLY when the
        # decode chain is ready to push its first buffer, not before.
        # `audiomixer` (GstAggregator) refuses to output anything until
        # every linked sink pad has produced at least one buffer -- and
        # webrtcbin's rtpjitterbuffer + decode takes anywhere from a few
        # hundred ms to a couple of seconds to deliver the first buffer,
        # depending on network conditions. If we link master_mixer_pad
        # before that first buffer is ready, the studio-monitor chain
        # goes silent for the entire wait -- observed live as "a couple
        # seconds of mute on connect" on mobile-network connections.
        # Fix: install a BLOCK probe on gate_conv's src pad; the probe
        # fires when the first real buffer is about to be pushed
        # downstream, and only then do we request the master_mixer sink
        # pad and link it in. The probe then removes itself so the
        # first buffer and everything after flows normally.
        gate_conv_src = gate_conv.get_static_pad("src")
        def _on_first_buffer_ready(probed_pad, info, _u):
            s = self.remote_dj_session
            if s is not session or s.master_mixer_pad is not None:
                # Session was already stopped or the pad was linked in a
                # race -- either way, just unblock.
                return Gst.PadProbeReturn.REMOVE
            s.master_mixer_pad = self.master_mixer.request_pad_simple("sink_%u")
            probed_pad.link(s.master_mixer_pad)
            print("  Remote DJ: real mic audio linked to master_mixer")
            return Gst.PadProbeReturn.REMOVE
        gate_conv_src.add_probe(
            Gst.PadProbeType.BLOCK_DOWNSTREAM, _on_first_buffer_ready, None,
        )

        print("  Remote DJ: decode chain wired; waiting for first buffer to link mixer")
        session.real_buf_seen = True

    def _remote_dj_on_connection_state(self, element, _pspec):
        session = self.remote_dj_session
        if session is None or session.webrtc is not element:
            return
        state = element.props.connection_state
        print(f"  Remote DJ connection state: {state.value_nick}")
        if state in (GstWebRTC.WebRTCPeerConnectionState.FAILED, GstWebRTC.WebRTCPeerConnectionState.CLOSED):
            self._remote_dj_session_stop()

    def _remote_dj_set_gate(self, active):
        session = self.remote_dj_session
        if session is None or session.remote_gate is None:
            print("  remote_dj_gate requested but no session is active — ignoring")
            return
        session.remote_gate.set_property("volume", 1.0 if active else 0.0)
        self._apply_talk_ducking()
        self._apply_mic_mode_hold()
        print(f"  Remote DJ gate: {'ON' if active else 'OFF'}")

    def _remote_dj_session_stop(self):
        session = self.remote_dj_session
        if session is None:
            return False
        print("  Remote DJ: session stopping")
        self.remote_dj_session = None

        # Safety first: close the gate before tearing anything down.
        if session.remote_gate is not None:
            session.remote_gate.set_property("volume", 0.0)
            self._apply_talk_ducking()
            # Also fold any mic-held Manual back to Auto now that the
            # remote is definitively off -- session_stop is one of the
            # implicit "gate goes off" paths where _remote_dj_set_gate
            # isn't called.
            self._apply_mic_mode_hold()

        # Teardown ordering that avoids master_mixer freeze:
        # (1) UNLINK the master_mixer sink pad from its upstream peer
        #     BEFORE setting anything to NULL. If we set upstream to NULL
        #     while master_mixer's sink pad is still linked to it,
        #     master_mixer's aggregator thread can end up trying to pull
        #     buffers from a NULL-state pad and freeze -- observed live
        #     during Stage 6 as "disconnect kills program audio". Same
        #     lesson _on_mic_error's docstring already flagged for the
        #     local mic bin ("unlinking a live-mixer-linked bin mid-flight
        #     is the riskier operation, reserved for planned transitions");
        #     a remote-DJ hangup is one of those planned transitions but
        #     the release half specifically needed this ordering fix.
        # (2) Release the master_mixer sink pad next -- it's now orphaned
        #     (unlinked upstream) so it can be released safely.
        # (3) Same pattern for monitor_tee_pad on the outbound branch.
        # (4) Only THEN set upstream elements to NULL and remove them.
        if session.master_mixer_pad is not None:
            peer = session.master_mixer_pad.get_peer()
            if peer is not None:
                peer.unlink(session.master_mixer_pad)
            self.master_mixer.release_request_pad(session.master_mixer_pad)
        if session.monitor_tee_pad is not None:
            peer = session.monitor_tee_pad.get_peer()
            if peer is not None:
                session.monitor_tee_pad.unlink(peer)
            self.remote_dj_tee.release_request_pad(session.monitor_tee_pad)
        if session.local_mic_tee_pad is not None and self.local_mic_tee is not None:
            peer = session.local_mic_tee_pad.get_peer()
            if peer is not None:
                session.local_mic_tee_pad.unlink(peer)
            self.local_mic_tee.release_request_pad(session.local_mic_tee_pad)

        for el in session.elements:
            el.set_state(Gst.State.NULL)
        for el in session.elements:
            self.main_pipeline.remove(el)

        if self._remote_dj_server:
            self._remote_dj_server.disconnect_threadsafe()

        print("  Remote DJ: session stopped")
        return False

    def _start_duck_ramp(self, target):
        """Ramps self.duck_gain's volume property smoothly toward
        `target` over DUCK_RAMP_MS, via a plain recurring GLib timeout
        (not Gst.Controller -- this codebase has no existing use of
        GStreamer's timed-value-automation API anywhere, and introducing
        it here for a single ~500ms fade would add real cognitive
        overhead -- clock-timestamp scheduling, control-binding
        lifecycle -- for no practical benefit over a plain imperative
        step timer, the same style already used everywhere else in this
        engine, e.g. _poll_position's own recurring callback). A
        ~20-25ms step cadence is imperceptibly smooth for a loudness
        fade -- this is not sample-accurate automation, it doesn't need
        to be, it just needs to avoid the click an instantaneous level
        change would cause.

        Cancelling/replacing an in-flight ramp (PTT toggled again before
        the previous ramp finished) is handled by reading duck_gain's
        CURRENT live value as the new ramp's start point -- it smoothly
        redirects toward the new target rather than jumping or fighting
        a second concurrent timer."""
        if self._duck_ramp_source_id is not None:
            GLib.source_remove(self._duck_ramp_source_id)
            self._duck_ramp_source_id = None
        start = self.duck_gain.get_property("volume")
        if abs(start - target) < 1e-4:
            return
        steps_done = [0]

        def _step():
            steps_done[0] += 1
            frac = min(1.0, steps_done[0] / DUCK_RAMP_STEPS)
            self.duck_gain.set_property("volume", start + (target - start) * frac)
            if frac >= 1.0:
                self._duck_ramp_source_id = None
                return False  # stop the GLib timer
            return True

        self._duck_ramp_source_id = GLib.timeout_add(DUCK_RAMP_MS // DUCK_RAMP_STEPS, _step)

    def _pause_deck(self, slot):
        if slot not in SLOTS:
            return
        deck = self.decks.get(slot)
        if not deck or deck.paused:
            return

        pos = self._get_deck_position(deck)
        deck.paused_position = pos
        deck.paused = True

        # Unlink from the mixer before pausing — leaving a paused
        # (non-producing) pad linked risks the aggregator stalling on
        # it and blocking the other deck's audio too, not just this
        # one's.
        try:
            deck.pipeline.get_static_pad("src").unlink(deck.mixer_pad)
        except Exception as exc:
            print(f"  [{slot}] pause: unlink failed: {exc}", flush=True)
        if deck.mixer_pad is not None:
            self.mixer.release_request_pad(deck.mixer_pad)
        deck.mixer_pad = None

        ret = deck.pipeline.set_state(Gst.State.PAUSED)
        print(f"  [{slot}] Paused at {pos:.1f}s (set_state PAUSED -> {ret})", flush=True)

    def _resume_deck(self, slot):
        """Rather than reviving the exact same Gst.Bin after unlinking
        it (that path reported PLAYING/linked=OK but the position never
        advanced again — root cause not pinned down, and not worth
        blocking on), tear it down and create a fresh bin for the same
        log_item via the normal (well-proven) deck-creation path, then
        seek it to where it was paused. Two independently-verified
        mechanisms — track-transition creation and manual seek — doing
        the work instead of one untested one."""
        if slot not in SLOTS:
            return
        deck = self.decks.get(slot)
        if not deck or not deck.paused:
            return

        resume_position = deck.paused_position
        log_item = deck.log_item
        self._remove_deck(deck)

        new_deck = self._create_deck(
            slot, log_item, resume_position_ns=int(resume_position * Gst.SECOND)
        )
        if new_deck is None:
            print(f"  [{slot}] Resume failed — could not recreate deck", flush=True)
            return

        new_deck.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            int(resume_position * Gst.SECOND),
        )

        with self._lock:
            self.decks[slot] = new_deck
        self._deck_bin_map[id(new_deck.pipeline)] = new_deck

        bus = new_deck.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_deck_error, new_deck)

        print(f"  [{slot}] Resumed at {resume_position:.1f}s", flush=True)

    def _seek_deck(self, slot, position):
        """Mirrors _resume_deck's approach: a flushing seek on a deck
        bin that's already linked into the live mixer deadlocked in
        testing badly enough that systemd needed a SIGKILL to recover
        (dead air the whole time) — root cause not chased down given
        the severity, same call as tonight's other "don't mutate the
        live bin" fixes. Tear the deck down and recreate it fresh at
        the target position instead of seeking in place."""
        if slot not in SLOTS:
            leading = self._leading_deck()
            slot = leading.slot if leading else None
        if slot not in SLOTS:
            return
        deck = self.decks.get(slot)
        if not deck:
            return

        was_paused = deck.paused
        log_item = deck.log_item
        self._remove_deck(deck)

        new_deck = self._create_deck(
            slot, log_item, resume_position_ns=int(position * Gst.SECOND)
        )
        if new_deck is None:
            print(f"  [{slot}] Seek failed — could not recreate deck", flush=True)
            return

        new_deck.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            int(position * Gst.SECOND),
        )

        with self._lock:
            self.decks[slot] = new_deck
        self._deck_bin_map[id(new_deck.pipeline)] = new_deck

        bus = new_deck.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_deck_error, new_deck)

        if was_paused:
            self._pause_deck(slot)
            new_deck.paused_position = position
        else:
            self._next_triggered = False

        print(f"  [{slot}] Seek to {position:.1f}s", flush=True)

    def _eject_deck(self, slot):
        if slot not in SLOTS:
            return
        deck = self.decks.get(slot)
        if deck:
            self._remove_deck(deck)
        print(f"  [{slot}] Ejected")
        self._start_next_track(slot=slot)

    def _reload_and_restart_current_log(self):
        """Tear down whatever's playing and switch to the current
        hour's approved log right away, instead of waiting for the
        natural end-of-track/end-of-hour transition. Used when
        something just replaced the current hour's log out from under
        the engine (e.g. a manual 'play this playlist now' request)."""
        close_old_connections()
        with self._lock:
            decks_to_remove = [d for d in self.decks.values() if d]
        for deck in decks_to_remove:
            self._remove_deck(deck)

        self._next_triggered = False
        now = timezone.localtime()
        self._load_log_for(now.date(), now.hour)
        if self.log_items:
            self._start_next_track()
            print(f"  Reloaded current-hour log by request — {len(self.log_items)} items")
        else:
            print("  Reload requested but no approved log for current hour")

    def _apply_audio_output_device(self, device):
        """Swap the alsasink output device live. alsasink's `device`
        property is fixed once the element is in PAUSED/PLAYING, so
        the pipeline drops to READY for the change. Brief audio
        dropout (~tens of ms) while the device reopens."""
        if not self.alsasink or not self.main_pipeline:
            return False
        try:
            current = self.alsasink.get_property("device")
        except Exception:
            current = None
        if current == device:
            return False
        print(f"  Switching alsasink device {current} -> {device}")
        self.main_pipeline.set_state(Gst.State.READY)
        self.alsasink.set_property("device", device)
        self.main_pipeline.set_state(Gst.State.PLAYING)
        return True

    def _reload_queue_if_changed(self):
        if not self.current_log:
            return
        now = time.time()
        if now - self._last_queue_reload < 3.0:
            return
        self._last_queue_reload = now

        close_old_connections()
        fresh_items = list(
            self.current_log.items
            .select_related("track", "track__artist", "track__album", "track__category", "track__category__kind")
            .order_by("position")
        )
        occupied_ids = {d.log_item.id for d in self.decks.values() if d}

        if occupied_ids:
            # Cursor = "position right after the last item that has EITHER
            # already aired (played_at set) OR is currently on a deck."
            # The played_at half is load-bearing for short spots: e.g. a
            # 4-second WxTemp Current Temp can finish and free its deck
            # BEFORE the outgoing longer track hits its own crossfade
            # trigger, at which point occupied_ids collapses back to just
            # the outgoing track. Considering only occupied_ids at that
            # moment walks the cursor BACKWARDS to right after the
            # outgoing track (i.e., to the just-finished spot's own
            # position), and the very next _poll_position tick re-fires
            # the crossfade trigger with the same target -- an audible
            # back-to-back double play of the same short spot. Anchoring
            # on played_at pins the cursor forward as soon as a spot has
            # aired, whether or not it's still on a deck.
            new_cursor = 0
            for i, item in enumerate(fresh_items):
                if item.id in occupied_ids or item.played_at is not None:
                    new_cursor = i + 1
        else:
            # Nothing on either deck (e.g. manual-mode hold, or a brief
            # idle window between tracks). The deck-based rule above
            # would collapse to 0 here, which loops back to the top of
            # the hour (usually the Legal ID) as soon as playback
            # resumes -- so preserve the existing cursor instead. If
            # DB-side items shifted position, follow the item that was
            # at the old cursor to its new index; if the cursor was
            # already past the end, keep it past the end.
            prev_next = (
                self.log_items[self._queue_cursor]
                if 0 <= self._queue_cursor < len(self.log_items)
                else None
            )
            if prev_next is not None:
                new_cursor = next(
                    (i for i, item in enumerate(fresh_items) if item.id == prev_next.id),
                    self._queue_cursor,
                )
            else:
                new_cursor = self._queue_cursor

        self.log_items = fresh_items
        self._queue_cursor = min(new_cursor, len(fresh_items))

    def _check_stuck_decks(self):
        """Watchdog for a deck whose EOS was never detected. Seen live: a
        startup-timing race left both decks permanently deadlocked --
        _poll_position's crossfade trigger only runs when the OTHER slot
        is empty (see the `if not other_occupied` guard below), which is
        correct for the normal case (EOS frees a slot, the survivor's own
        trigger then fires) but has no escape if EOS never fires for
        EITHER deck: neither slot ever frees, so neither trigger can ever
        run, and playback silently stalls forever at the end of both
        tracks with no error anywhere -- state-writing and command
        handling keep ticking normally since they don't touch this path,
        so nothing else looks wrong.

        Recovery is deliberately NOT the normal-EOS teardown path
        (_handle_deck_finished -> _remove_deck's synchronous
        pipeline.set_state(Gst.State.NULL) / main_pipeline.remove() /
        release_request_pad()) -- confirmed live that those GStreamer
        calls can themselves block forever on a pipeline that's already
        wedged (which is exactly the state a deck stuck here is in): a
        first version of this watchdog that called _handle_deck_finished
        hung the entire engine process so completely that even SIGTERM
        was ignored for systemd's full 90s stop timeout, requiring
        SIGKILL. Instead this only forgets the deck at the application
        level, so _start_next_track can claim the slot and build a fresh
        deck with its own new mixer pad. The old, wedged GStreamer bin is
        deliberately leaked (stays linked into the mixer, silent, using a
        small amount of memory) rather than risking another full hang --
        a normal engine restart clears it. _next_triggered is reset too:
        it's a single engine-wide flag, and it stayed pinned True for the
        whole stuck period (set once, when the two boot decks were first
        created), which would otherwise silently block the survivor's own
        next trigger even after this frees its slot."""
        for slot, deck in list(self.decks.items()):
            if deck is None or deck.finished or deck.paused:
                continue
            duration = deck.track.duration_seconds or 0
            if not duration:
                continue
            pos = self._get_deck_position(deck)
            if pos > duration + DECK_STUCK_TIMEOUT_SECONDS:
                print(f"  [{slot}] WATCHDOG: '{deck.track.title}' stuck at {pos:.1f}s "
                      f"(duration {duration:.1f}s) with no EOS detected -- abandoning "
                      f"this deck (leaking its pipeline rather than risking a hung "
                      f"teardown) and freeing the slot.")
                deck.finished = True
                self.decks[slot] = None
                self._next_triggered = False

    def _poll_position(self):
        if not self.running:
            return False

        self._check_commands()
        self._check_stuck_decks()
        self._reload_queue_if_changed()

        leading = self._leading_deck()
        if not leading or leading.finished:
            self._write_state()
            return True

        # Don't check trigger until deck has been playing for at least 5 seconds
        deck_age = time.time() - (leading.started_at or time.time())
        if deck_age < 5.0:
            self._write_state()
            return True

        pos = self._get_deck_position(leading)
        track = leading.track

        next_start = track.next_start_seconds
        if next_start is None:
            next_start = track.duration_seconds or 0

        # Sanity: position must be reasonable (> 5s, < track duration + buffer)
        max_pos = (track.duration_seconds or 3600) + 10
        if pos <= 5.0 or pos > max_pos:
            self._write_state()
            return True

        other_slot = self._other_slot(leading.slot)
        other_occupied = self.decks[other_slot] is not None

        if not other_occupied:
            next_item = None
            if self._queue_cursor < len(self.log_items):
                next_item = self.log_items[self._queue_cursor]
            else:
                # Current hour's queue is exhausted — peek at the next
                # hour's already-approved log so the last track of the
                # hour can still crossfade into the first track of the
                # next, instead of always hard-cutting at top of hour.
                peek = self._peek_next_hour()
                if peek:
                    _, next_hour_items = peek
                    next_item = next_hour_items[0] if next_hour_items else None
                elif self._try_extend_live_log():
                    # Real next hour isn't built yet, but a Log Fill
                    # Configuration re-pick just landed on the live log —
                    # pick it up now so the crossfade below can trigger
                    # into it normally instead of waiting for EOS.
                    next_item = self.log_items[self._queue_cursor]

            if next_item is not None:
                next_cue_in = next_item.track.cue_in_seconds or 0.0
                trigger_point = next_start - next_cue_in

                if trigger_point < 10.0:
                    trigger_point = next_start

                if pos >= trigger_point - CACHE_WARM_LEAD_SECONDS:
                    self._warm_track_cache(next_item)

                if not self._next_triggered and pos >= trigger_point:
                    if self.manual_mode:
                        # DJ is holding for a talk-over -- remember the
                        # handoff was due so _set_manual_mode can fire it
                        # immediately once flipped back to Auto, instead
                        # of cutting off the current track early.
                        self._manual_hold_pending = True
                    else:
                        print(f"  Trigger: pos={pos:.1f}s >= trigger={trigger_point:.1f}s, starting next ({next_item.track.title}) on deck {other_slot}")
                        self._next_triggered = True
                        self._start_next_track(slot=other_slot)

        self._write_state()
        return True

    def _on_deck_eos_probed(self, deck_bin):
        deck = self._deck_bin_map.get(id(deck_bin))
        if not deck or deck.finished:
            return
        self._handle_deck_finished(deck)

    def _on_deck_error(self, bus, message, deck):
        err, debug = message.parse_error()
        print(f"  GStreamer error on {deck.track.title}: {err} ({debug})")
        GLib.idle_add(self._handle_deck_finished, deck)
        return True

    def _handle_deck_finished(self, deck):
        slot = deck.slot
        self._remove_deck(deck)
        other_deck = self.decks[self._other_slot(slot)]
        if other_deck is not None:
            # The crossfade already handed off to the other slot before
            # this one finished — nothing more to do, it's playing.
            return
        if self.manual_mode:
            # DJ is holding for a talk-over and the song ran out before
            # they flipped back to Auto -- leave the slot empty (mic-only)
            # rather than starting the next track out from under them.
            self._manual_hold_pending = True
            return
        # Nothing had triggered yet (e.g. a track too short to ever hit
        # the crossfade trigger) — this was the only thing playing, so
        # start the next queued item now, in the slot that just freed up.
        self._start_next_track(slot=slot)

    def _on_log_exhausted(self, slot):
        print(f"  [{slot}] Log exhausted for this hour.")
        now = timezone.localtime()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        self._load_log_for(next_hour.date(), next_hour.hour)
        if self.log_items:
            self._start_next_track(slot=slot)
        else:
            print("No approved log for next hour. Waiting...")
            GLib.timeout_add_seconds(30, self._try_load_next_hour)

    def _try_load_next_hour(self):
        if not self.running:
            return False
        now = timezone.localtime()
        self._load_log_for(now.date(), now.hour)
        if self.log_items:
            self._start_next_track()
            return False
        return True

    def _write_now_playing(self, track):
        """Tells the stream encoders (see encoders/services/encoder_manager.py,
        which watches this exact file via Liquidsoap's file.watch) what's
        currently playing, so listener apps can show real title/artist
        instead of just the static station name. Called from
        _create_deck() -- there's no single unambiguous "audible this
        exact instant" moment during a crossfade (both decks are briefly
        non-paused simultaneously), so this is a deliberate, standard-
        practice approximation: update at deck-creation time rather than
        chase an inherently fuzzy exact instant.

        Honors Track.alt_send_enabled/alt_send_text (the RBDS RadioText
        override) so the stream's metadata already matches whatever the
        eventual real RBDS pipeline will send, rather than needing the
        two to be reconciled after the fact."""
        if track.alt_send_enabled and track.alt_send_text:
            title, artist = track.alt_send_text, ""
        else:
            title = track.title
            artist = track.artist.name if track.artist else ""
        # timestamp must be a string, not a float -- Liquidsoap's
        # metadata.json.parse() infers a strict type from the JSON shape
        # (confirmed live) and insert_metadata() requires [(string*string)];
        # a numeric field breaks parsing with "cannot be parsed as type
        # {timestamp: string, _}".
        payload = {"title": title, "artist": artist, "timestamp": str(time.time())}
        # Deliberately NOT the usual atomic tmp-write-then-rename pattern
        # (see hardware/signals.py's _write_engine_command) -- verified
        # live that Liquidsoap's file.watch only survives the FIRST
        # rename-replace of its watched path; every rename after that
        # watches a dead inode and never fires again. A plain in-place
        # write (same inode every time) fired reliably across many
        # consecutive changes in the same live test. The tradeoff (a
        # reader could see a half-written file) is acceptable here: the
        # payload is a few dozen bytes (effectively a single write())
        # and this is already a best-effort approximation, not a
        # hard-guarantee feature.
        try:
            NOW_PLAYING_PATH.parent.mkdir(parents=True, exist_ok=True)
            NOW_PLAYING_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            # /run/isadoraair not created yet -- matches
            # hardware/signals.py's _write_engine_command's own convention.
            pass

    def _write_state(self, transport="PLAYING"):
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

            with self._lock:
                snapshot = dict(self.decks)

            decks_out = {}
            for slot in SLOTS:
                deck = snapshot[slot]
                if not deck:
                    decks_out[slot] = None
                    continue
                pos = self._get_deck_position(deck)
                t = deck.track
                decks_out[slot] = {
                    "track_id": t.id,
                    "title": t.title,
                    "artist": t.artist.name if t.artist else "",
                    "album": t.album.title if t.album else "",
                    "position": round(pos, 1),
                    "duration": t.duration_seconds or 0,
                    "next_start": t.next_start_seconds,
                    "cue_in": t.cue_in_seconds or 0,
                    "category": t.category.code if t.category else "",
                    "format": t.format or "",
                    "paused": deck.paused,
                }

            # Seconds-from-now ETA for each queue item, so the UI can
            # show a live "time on" clock estimate. Walks forward from
            # whatever time is left on the currently-leading deck,
            # accumulating each subsequent track's "effective" play
            # time (next_start_seconds if set, else full duration) —
            # the same heuristic the crossfade trigger itself uses, so
            # the estimate matches how handoffs actually happen.
            leading = None
            for slot in SLOTS:
                d = snapshot[slot]
                if d and not d.paused:
                    leading = d
                    break

            eta = 0.0
            if leading:
                lt = leading.track
                lpos = self._get_deck_position(leading)
                l_effective = lt.next_start_seconds if lt.next_start_seconds is not None else (lt.duration_seconds or 0)
                eta = max(0.0, l_effective - lpos)

            queue = []
            for qi in self._get_upcoming_preview():
                qt = qi.track
                queue.append({
                    "item_id": qi.id,
                    "track_id": qt.id,
                    "title": qt.title,
                    "artist": qt.artist.name if qt.artist else "",
                    "duration": qt.duration_seconds or 0,
                    "category": qt.category.code if qt.category else "",
                    "format": qt.format or "",
                    "fill_color": qt.category.kind.fill_color if qt.category else None,
                    "eta_seconds": round(eta, 1),
                })
                effective = qt.next_start_seconds if qt.next_start_seconds is not None else (qt.duration_seconds or 0)
                eta += effective

            state = {
                "transport": transport,
                "decks": decks_out,
                "queue": queue,
                "queue_cursor": self._queue_cursor,
                "total_items": len(self.log_items),
                "log_id": self.current_log.id if self.current_log else None,
                "hour": self.current_log.hour if self.current_log else None,
                "date": self.current_log.date.isoformat() if self.current_log else None,
                "timestamp": time.time(),
                # mic_configured distinguishes "never wired up" (button
                # disabled) from "wired up but off"; mic_ok distinguishes
                # "configured but currently erroring" from healthy;
                # mic_live is the actual gate state -- the dashboard must
                # reflect this, not optimistic client-side state.
                "mic_configured": self.mic_ptt_valve is not None,
                "mic_ok": self.mic_ok,
                "mic_live": self.mic_live,
                "manual_mode": self.manual_mode,
                "manual_from_mic": self._manual_from_mic,
                # Same "configured vs. live" distinction as the mic fields
                # above -- remote_dj_configured means the feature is built
                # into this pipeline at all (RemoteDJConfig.enabled at
                # startup), remote_dj_connected means a DJ is actually
                # connected right now, remote_dj_live is the gate state.
                "remote_dj_configured": self.remote_dj_tee is not None,
                "remote_dj_connected": self.remote_dj_session is not None,
                "remote_dj_live": bool(
                    self.remote_dj_session and self.remote_dj_session.remote_gate
                    and self.remote_dj_session.remote_gate.get_property("volume") > 0.0
                ),
            }

            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.rename(STATE_PATH)
        except Exception as exc:
            print(f"Failed to write state: {exc}")
