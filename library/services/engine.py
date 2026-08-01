import functools
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
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

from contextlib import contextmanager

from django.db import close_old_connections, connection, transaction
from django.db.models import F
from django.db.utils import OperationalError
from django.utils import timezone
from hardware.models import AudioInput, AudioOutput, AudioPipeline, DuckingConfig, RemoteDJAudioInput
from library.models import Category, FXBusConfig, FXCart, LogItem, PlayEvent, PlaylistLog, RemoteDJConfig, Track, VoiceTrack, VoiceTrackConfig
from monitoring.models import emit_event
from webrequests.models import SongRequest
from library.services.log_builder import (
    DURATION_FIT_MARGIN,
    LOCK_CONTENDED,
    append_fill_items,
    build_and_approve_hour_log_locked,
    fill_remaining_hour,
)
from library.services.remote_dj_signaling import RemoteDJSignalingServer
from webrequests.services import SCHEDULING_CONTENDED, maybe_schedule_song_request, mark_song_requests_aired

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
# Deliberately a SEPARATE file from NOW_PLAYING_PATH above, not an
# extra key folded into that JSON -- NOW_PLAYING_PATH is also consumed
# by the stream encoders' Liquidsoap file.watch() (see
# _write_now_playing's own docstring: it's written in-place, non-
# atomically, because file.watch() only survives the FIRST rename-
# replace of its watched path). Keeping RBDS's own state in its own
# file means this feature can never risk that fragile mechanism, and
# RBDS reads it by plain poll (once per its own 1s tick), not
# file.watch, so it doesn't need the same in-place-write care.
RBDS_CATEGORY_STATE_PATH = Path("/run/isadoraair/rbds_category_state.json")

# Remote DJ diagnostic instrumentation -- see class RemoteDJSession's
# docstring and _remote_dj_on_pad_added. Truncated + reopened on every
# session start; left in place after session stop so a post-mortem
# can inspect the last session's data if the user reports static.
DJ_DIAG_LOG = Path("/run/isadoraair/remote_dj_diag.log")
# Raw S16LE stereo 44100 -- can be replayed via
# `ffplay -f s16le -ar 44100 -ac 2 /run/isadoraair/remote_dj_first_1s.pcm`
# or examined in Audacity by importing as raw. Continuous session-length
# dump of every buffer that reaches master_mixer via the DJ pad. Kept the
# `_first_1s` name for backward compatibility of any monitoring/muscle
# memory; it now covers the whole session, but a session is usually short
# enough that the file stays modest (~10 MB/min at S16 stereo 44100).
DJ_DUMP_PCM = Path("/run/isadoraair/remote_dj_first_1s.pcm")


def _dj_diag(session, msg):
    """Timestamped append to the remote-DJ diagnostic log. No-op if the
    session's diag file couldn't be opened (permissions / disk full)."""
    if session is None or getattr(session, "diag_fh", None) is None:
        return
    try:
        session.diag_fh.write(f"{time.time():.3f} {msg}\n")
        session.diag_fh.flush()
    except OSError:
        pass

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

# Remote DJ over WebRTC.
# Opus's RTP payload mandates 48kHz per RFC 7587 regardless of
# AudioPipeline.sample_rate -- this is NOT the same as pipeline_sample_rate
# and must not be confused with it.
REMOTE_DJ_OPUS_RATE = 48000
REMOTE_DJ_OPUS_FRAME_SIZE_MS = 10  # over the 20ms default, to shave latency
# Small, leaky-upstream buffer for the monitor-return branch -- this is
# the latency-critical path the whole feature exists for; leaky-upstream
# so it can only ever drop its own data, same reasoning as stereotool_queue.
REMOTE_DJ_MONITOR_QUEUE_MS = 250


def _log_item_playable(log_item):
    """(bool, reason) -- is this queued log item something we can actually
    hand to GStreamer? A log item can be "unplayable" for a few reasons:

      * The row's Track FK was NULLed because the track was deleted from
        the library (PlaylistLogItem.track is on_delete=SET_NULL, which
        preserves the historical log without blocking Track deletion --
        by design, but callers must handle the null).
      * The Track row still exists but its `filepath` was cleared.
      * The file was moved/deleted off disk since the log was built.

    Consumers should skip unplayable items rather than crash on them --
    an uncaught AttributeError inside a GLib callback (esp. the position
    poller) hangs the engine off-air, since GLib silently unschedules
    a callback that raised. See _glib_safe / _next_queue_item / etc.
    """
    if log_item is None:
        return False, "log_item is None"
    track = log_item.track
    if track is None:
        return False, "track deleted from library (FK set NULL)"
    fp = track.filepath
    if not fp:
        return False, f"track id={track.id} has no filepath"
    if not Path(fp).is_file():
        return False, f"file missing on disk: {fp}"
    return True, None


def _glib_safe(default_return=True):
    """Wrap a GLib timer/idle/bus callback so an unhandled exception
    prints a traceback but doesn't silently unschedule the callback.

    Timer callbacks (GLib.timeout_add*) MUST return True to stay armed
    -- an exception counts as "returned None", i.e. "unschedule me",
    which is exactly how a lone bug in _poll_position (e.g. dereferencing
    a NULLed track FK on a queued log item) can hang the whole engine
    off-air with no error surfaced anywhere except a swallowed
    stderr write. Idle-add callbacks are one-shots and can default to
    False; the caller picks per-callsite.

    Every catch is ALSO recorded as a SystemEvent (level=error,
    category=engine) so the operator sees it on /monitoring/ instead of
    only in journalctl -- the whole point of catching a callback bug
    is that it stops mattering to *audio*; still needs to matter to
    *someone*.

    Also refuses to swallow KeyboardInterrupt / SystemExit -- those must
    still tear down the process on Ctrl-C / SIGTERM.
    """
    def decorator(method):
        @functools.wraps(method)
        def wrapper(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                print(f"  [engine] Uncaught exception in {method.__qualname__}:")
                traceback.print_exc()
                emit_event(
                    category="engine",
                    level="error",
                    title=f"Uncaught exception in {method.__qualname__}",
                    detail={
                        "exception": exc,
                        "traceback": traceback.format_exc(),
                        "callback": method.__qualname__,
                    },
                    dedupe_key=f"engine|glib_safe|{method.__qualname__}",
                )
                return default_return
        return wrapper
    return decorator


# Number of concurrent remote-DJ slots the pipeline is built to
# support. Hardcoded to 1 today; the audio-path structure (per-slot
# persistent selector + silence source + gate + gain + master_mixer
# pad) is a list so bumping to N is a constant change plus signaling/
# UI/policy work that lives outside engine.py. Do NOT flip this to
# >1 without matching signaling-server work; the engine will happily
# build the slots but only one WebRTC session can be active at a time
# with the current signaling protocol.
MAX_DJ_SLOTS = 1


class RemoteDJSlot:
    """Persistent per-slot audio-path state, allocated ONCE at engine
    startup in _build_main_pipeline and never released until the
    engine process exits. The mixer:src use-after-free class of bug
    that manifested as static-on-connect (kernel-confirmed SEGVs
    2026-07-14 00:07 in orcexec / 2026-07-22 07:57 in libc memcpy,
    both on master_mixer:src thread) came from calling
    master_mixer.request_pad_simple at RUNTIME on a live aggregator.
    Persistent-slot design eliminates that race by construction:
    the slot's master_mixer pad is requested here at startup and
    never mutated for the pipeline's lifetime; DJ connect/disconnect
    only flips this slot's input-selector active-pad between the
    silence source (idle) and the WebRTC decode chain (live), which
    is a plain property set with no aggregator-state impact."""

    def __init__(self, slot_id, selector, silence_pad, webrtc_pad,
                 remote_gain, remote_gate, master_mixer_pad, silence_src):
        self.slot_id = slot_id
        self.selector = selector             # GstInputSelector element
        self.silence_pad = silence_pad       # selector's sink_0 (silence path)
        self.webrtc_pad = webrtc_pad         # selector's sink_1 (WebRTC path, pre-allocated)
        self.remote_gain = remote_gain       # volume element (persistent, applied per-session gain)
        self.remote_gate = remote_gate       # volume element (persistent, 0.0 or 1.0)
        self.master_mixer_pad = master_mixer_pad  # never released
        self.silence_src = silence_src       # audiotestsrc wave=silence
        self.session = None                  # the RemoteDJSession currently occupying this slot, or None


class RemoteDJSession:
    """Holds every GStreamer element reference for the one active
    remote-DJ WebRTC session -- mirrors the Deck class's role for a
    playback slot (a thin data holder; the actual lifecycle logic lives
    in PlaybackEngine's _remote_dj_* methods, same split as
    Deck/_create_deck/_remove_deck).

    Slot bookkeeping (2026-07-22 refactor): the DJ audio-path pieces
    that must be persistent for the pipeline's whole lifetime -- the
    input-selector, its two sink pads, the gain/gate volume elements,
    and the master_mixer pad -- live on a RemoteDJSlot allocated at
    engine startup, NOT here. This session tracks only which slot it
    took (session.slot_id) plus its ephemeral webrtcbin + decode
    subchain that gets linked into the slot's selector.sink_1 at
    connect and torn down at disconnect. The slot itself is
    unaffected by session lifecycle.

    MAX_DJ_SLOTS is 1 today; the shape is generalizable to N."""
    def __init__(self):
        self.webrtc = None
        self.ice_agent = None
        self.slot_id = None            # which self.dj_slots[] index this session took
        self.master_mixer_pad = None   # legacy field, unused in the new design (kept so old teardown paths don't NPE if reached)
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
        # The DJ's most recent gate command. `remote_gate.volume` is the
        # PHYSICAL gate state (may be forced to 0 during a transient
        # WebRTC disconnect to keep static out of the mixer); this
        # remembers what the user WANTED so we can restore it if the
        # connection recovers without them touching anything. See
        # _remote_dj_on_connection_state.
        self.gate_desired = False
        # Diagnostic instrumentation added 2026-07-20 to trap evidence
        # of the still-unresolved "playing deck goes to static when a
        # remote-DJ connects" bug. Everything below is per-session
        # state that's opened/wired at session_start and cleaned up at
        # session_stop. None of it affects audio behavior -- pure
        # observation.
        self.diag_fh = None            # open file handle for the diag log
        self.dump_fh = None            # open file handle for the PCM dump
        self.dump_bytes_written = 0    # running total for progress logging
        self.dump_last_marked_bytes = 0  # when we last emitted a progress line
        self.dj_level = None           # `level` element between opusdec and audioconvert
        self.dj_mixer_pad_src = None   # gate_conv src pad for probe cleanup
        self.caps_probe_id = 0         # pad probe id for the caps-event logger
        self.dump_probe_id = 0         # pad probe id for the PCM dump


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
        # PlayEvent row id written at _create_deck; closed out (ended_at
        # + duration_played_seconds) at _remove_deck. None if the write
        # failed at deck creation -- in which case no close-out attempt
        # is made either, avoiding a spurious update against a row that
        # doesn't exist.
        self.play_event_id = None


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
        # Populated in _build_main_pipeline when RemoteDJConfig.enabled
        # is true. MAX_DJ_SLOTS entries; each holds a persistent audio
        # subchain that owns its own master_mixer sink pad for the
        # engine process's whole lifetime. Empty list when the feature
        # is disabled -- no slots, no persistent silence sources burning
        # cycles for nothing.
        self.dj_slots = []
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
        self._building_hours = set()  # {(date, hour)} currently being async-built -- guarded by self._lock
        # Web-request dedication intros: items that must play next
        # regardless of self.log_items/rollover -- see
        # _maybe_insert_dedication_intro/_restore_followup_for_intro.
        # Checked by _next_queue_item BEFORE normal cursor logic.
        self._forced_next_items = []
        self._urgent_retry_counts = {}  # {category_code: count} -- per-category, not shared

    def start(self):
        self.running = True
        # Bookmark from previous instance MUST be read BEFORE the first
        # _create_deck call (via _start_next_track), otherwise the deck
        # gets built without the auto-resume seek and only a subsequent
        # deck load (much later, at track boundary) picks up the hint.
        self._read_resume_hint()
        self._build_main_pipeline()
        self._load_current_hour_log()
        # If the resume hint carries a log_item_id that lives in the
        # just-loaded queue, back the cursor up so that item loads
        # first -- otherwise a mid-crossfade snapshot would resume at
        # the deck-B item (cursor was already advanced past deck A's
        # item at crossfade-time) and deck A's remaining audio would
        # be skipped. See _apply_resume_hint_queue_rewind.
        self._apply_resume_hint_queue_rewind()
        self._restore_dedication_sequence_from_resume_hint()

        if not self.log_items and not self._forced_next_items:
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

    def _build_dj_slots(self, n_slots):
        """Build `n_slots` persistent DJ subchains, each terminating in a
        dedicated master_mixer sink pad requested HERE at engine startup
        and never released. See RemoteDJSlot's docstring for the class of
        bug this eliminates (mixer:src use-after-free from dynamic pad
        add/remove on the running aggregator).

        Per-slot shape:

            [audiotestsrc wave=silence]
                    │
                    ▼
            [capsfilter S16LE 44.1k stereo]
                    │
                    ▼
            input-selector  ← sink_0 (silence path, active by default)
                            ← sink_1 (WebRTC path, pre-allocated,
                                       upstream linked at session start)
                    │
                    ▼
            [audioconvert] → [volume gain] → [volume gate] → [audioconvert]
                    │
                    ▼
            master_mixer's dedicated pad for this slot

        The gate defaults to 0 (closed); it opens to 1 only while the DJ
        is actually broadcasting, same semantics as the pre-refactor
        session.remote_gate. Gain reads RemoteDJAudioInput.load().gain_db
        at connect time (see _remote_dj_on_pad_added), same as before.

        Silence source uses samplesperbuffer matched to ~23 ms at 44.1k,
        keeping the input-selector fed with steady periodic buffers that
        are well-timed for the switchover to WebRTC input. is-live=true
        so the source produces at wall-clock rate rather than as fast as
        possible."""
        pipe_caps = Gst.Caps.from_string(
            f"audio/x-raw,format=S16LE,rate={self.pipeline_sample_rate},"
            f"channels=2,layout=interleaved"
        )
        for slot_id in range(n_slots):
            silence_src = Gst.ElementFactory.make("audiotestsrc", f"dj_slot_{slot_id}_silence")
            # wave=4 = "silence" per the GstAudioTestSrcWave enum.
            silence_src.set_property("wave", 4)
            silence_src.set_property("is-live", True)
            silence_src.set_property("samplesperbuffer", 1024)  # ~23ms at 44.1k
            silence_caps = Gst.ElementFactory.make("capsfilter", f"dj_slot_{slot_id}_silence_caps")
            silence_caps.set_property("caps", pipe_caps)

            selector = Gst.ElementFactory.make("input-selector", f"dj_slot_{slot_id}_selector")
            # Each input has its own timeline (silence is live at wall
            # clock; WebRTC decoded audio has its own rtpjitterbuffer-
            # driven timing). sync-streams=false tells the selector not
            # to try to time-align across inputs; the mixer downstream
            # is what actually re-times to the master clock.
            selector.set_property("sync-streams", False)

            post_conv = Gst.ElementFactory.make("audioconvert", f"dj_slot_{slot_id}_post_conv")
            remote_gain = Gst.ElementFactory.make("volume", f"dj_slot_{slot_id}_gain")
            remote_gain.set_property("volume", 1.0)  # updated per-session from RemoteDJAudioInput.gain_db
            remote_gate = Gst.ElementFactory.make("volume", f"dj_slot_{slot_id}_gate")
            remote_gate.set_property("volume", 0.0)  # closed by default; opened via remote_dj_gate command
            out_conv = Gst.ElementFactory.make("audioconvert", f"dj_slot_{slot_id}_out_conv")

            for el in (silence_src, silence_caps, selector, post_conv,
                       remote_gain, remote_gate, out_conv):
                self.main_pipeline.add(el)

            # Silence path: source → caps → selector.sink_0
            silence_src.link(silence_caps)
            silence_sink = selector.request_pad_simple("sink_%u")
            silence_caps.get_static_pad("src").link(silence_sink)

            # WebRTC path: pre-allocate selector.sink_1 (empty upstream
            # at startup; _remote_dj_on_pad_added links a decode chain
            # into this pad when a session goes live, and unlinks at
            # session_stop). Pre-allocating this pad here at startup
            # means the selector's pad list is set-in-stone at the
            # moment the pipeline goes PLAYING -- the SAME "never
            # mutate a running element's pad list at runtime" invariant
            # we're enforcing on master_mixer, applied belt-and-braces
            # to input-selector too.
            webrtc_sink = selector.request_pad_simple("sink_%u")

            # Selector starts on the silence path.
            selector.set_property("active-pad", silence_sink)

            # Downstream: selector → post_conv → gain → gate → out_conv
            selector.link(post_conv)
            post_conv.link(remote_gain)
            remote_gain.link(remote_gate)
            remote_gate.link(out_conv)

            # THE pad that would otherwise be mutated at runtime by the
            # old dynamic-add code. Requested ONCE, right here, at
            # engine startup, on a mixer that hasn't yet entered
            # PLAYING. Zero race window.
            master_mixer_pad = self.master_mixer.request_pad_simple("sink_%u")
            out_conv.get_static_pad("src").link(master_mixer_pad)

            self.dj_slots.append(RemoteDJSlot(
                slot_id=slot_id,
                selector=selector,
                silence_pad=silence_sink,
                webrtc_pad=webrtc_sink,
                remote_gain=remote_gain,
                remote_gate=remote_gate,
                master_mixer_pad=master_mixer_pad,
                silence_src=silence_src,
            ))
            print(f"  Remote DJ slot {slot_id}: persistent audio subchain built + master_mixer pad allocated")

    def _dj_slot_available(self):
        """Return the first slot whose session is None, or None if all
        slots are occupied. With MAX_DJ_SLOTS=1 today this is either
        slot 0 or the caller falls through to 'already recording'
        semantics -- but the shape is right for N slots when we bump
        the constant."""
        for slot in self.dj_slots:
            if slot.session is None:
                return slot
        return None

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
        """Element messages from the shared main-pipeline bus. Handles
        BOTH the master output_level (dashboard VU meter) AND the
        remote-DJ session's dj_level (post-opusdec level meter, whose
        readings are appended to DJ_DIAG_LOG for the remote-DJ static
        post-mortem). All other element messages are ignored so this
        stays cheap."""
        structure = message.get_structure()
        if structure is None or structure.get_name() != "level":
            return True

        # Remote-DJ level meter -- log to diag file. Cheap, ~10Hz.
        session = self.remote_dj_session
        if session is not None and message.src is session.dj_level:
            try:
                peak = list(structure.get_value("peak")) or []
                rms = list(structure.get_value("rms")) or []
                _dj_diag(session,
                          f"dj_level peak={['%.1f' % p for p in peak]} rms={['%.1f' % r for r in rms]}")
            except Exception:
                pass
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

    def _on_dj_bus_msg(self, bus, message):
        """Bus WARNING and INFO messages routed to the remote-DJ diag
        log so a post-mortem can see what GStreamer was complaining
        about at the moment of a suspect static event. Filtered to
        messages whose src is anywhere under the current DJ session's
        webrtcbin or the DJ session's own decode-chain elements --
        so we don't spam the diag log with unrelated pipeline chatter.
        Errors from DJ elements land here too via message::error, but
        only get logged (not acted on) -- treating them as fatal would
        risk terminating the pipeline for what might be a benign
        transient.

        Whole body wrapped in try/except -- this runs on the GLib main
        loop and an unhandled exception here would kill the pipeline's
        entire event dispatch. Same rule as pad probes: diagnostic
        code must never take audio off-air."""
        try:
            return self._on_dj_bus_msg_inner(bus, message)
        except Exception as exc:
            print(f"  _on_dj_bus_msg suppressed exception: {exc!r}")
            return True

    def _on_dj_bus_msg_inner(self, bus, message):
        session = self.remote_dj_session
        if session is None or session.diag_fh is None:
            return True
        # Walk up the parent chain from message.src looking for a match
        # against our known DJ elements OR the webrtcbin. This catches
        # WARN/INFO from webrtcbin's internal children too (rtpbin,
        # jitterbuffer, dtls, etc.), which is exactly what we want for
        # a WebRTC-adjacent post-mortem.
        obj = message.src
        matched = False
        while obj is not None:
            if obj is session.webrtc or obj in session.elements:
                matched = True
                break
            try:
                obj = obj.get_parent()
            except Exception:
                break
        if not matched:
            return True

        mtype = message.type
        if mtype == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            _dj_diag(session, f"WARN from {message.src.get_name()}: {err} | {debug}")
        elif mtype == Gst.MessageType.INFO:
            err, debug = message.parse_info()
            _dj_diag(session, f"INFO from {message.src.get_name()}: {err} | {debug}")
        elif mtype == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            _dj_diag(session, f"ERROR from {message.src.get_name()}: {err} | {debug}")
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
        # vt_duck_gain sits between the deck bus (mixer) and duck_gain
        # -- a dedicated volume element for VT-driven ducking, distinct
        # from the mic/remote-DJ duck_gain so the two ducking policies
        # can be tuned independently. Starts at unity (1.0); the VT
        # state machine ramps it down when a VT fires and back up when
        # the sequence completes. See _vt_ramp_duck / _vt_apply_duck.
        self.vt_duck_gain = Gst.ElementFactory.make("volume", "vt_duck_gain")
        self.vt_duck_gain.set_property("volume", 1.0)
        # program_fx_mixer sums the (post-duck) deck bus with the FX bus
        # BEFORE the remote-DJ mix-minus tap, so FX carts and voice
        # tracks -- both fired through the FX submixer -- are audible
        # in the remote DJ's monitor return. Before this element
        # existed, FX joined master_mixer directly (downstream of the
        # remote_dj_tee), which meant the studio monitor heard the FX
        # but the remote DJ didn't. Reported live 2026-07-24 by the
        # user: "Test Drop plays through studio but not through the
        # connected remote-DJ session." Same routing fixes VT audio
        # over the mix-minus (which was invisible for the same
        # structural reason).
        self.program_fx_mixer = Gst.ElementFactory.make("audiomixer", "program_fx_mixer")
        self.master_mixer = Gst.ElementFactory.make("audiomixer", "master_mixer")

        elements = [
            self.mixer, self.vt_duck_gain, self.duck_gain,
            self.program_fx_mixer, self.master_mixer, convert, resample, capsfilter,
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
            # Enlarged ALSA ring on this sink specifically -- the Studio
            # Monitor sink stays at the driver default. Rationale: clicks
            # were being heard on the FM output and on the streams (both
            # fed downstream of StereoTool) but NOT on the studio monitor,
            # localizing the fault to this loopback-fed segment. Kernel
            # xrun counters were all zero at the times of audible clicks
            # -- consistent with StereoTool covering brief input starves
            # with its last-frame pad rather than surfacing an error.
            # Widening the writer-side ALSA ring here from the driver
            # default (~43 ms at the 5-period × 8.7-ms loopback default)
            # to ~200 ms gives ~4.6x more runway before a scheduler stall
            # on the engine's master_mixer:src thread starves the read
            # side. latency-time=20000 keeps ~10 periods per buffer so
            # ALSA still wakes the writer roughly every 20 ms. Cost is
            # ~160 ms extra one-way latency between the engine and
            # StereoTool -- inconsequential for a broadcast chain, the
            # studio-monitor branch is unaffected.
            self.stereotool_sink.set_property("buffer-time", 200000)
            self.stereotool_sink.set_property("latency-time", 20000)
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
        # Remote-DJ diagnostic bus watchers -- see _on_dj_bus_msg. These
        # route WARN/INFO/ERROR messages originating from a currently-
        # active remote-DJ session's elements (webrtcbin included) to
        # DJ_DIAG_LOG for the still-unresolved static-on-deck bug's
        # post-mortem. Wired once here at pipeline build time; the
        # handler itself no-ops when no session is active, so this is
        # zero overhead when the remote DJ isn't in use.
        bus.connect("message::warning", self._on_dj_bus_msg)
        bus.connect("message::info", self._on_dj_bus_msg)
        bus.connect("message::error", self._on_dj_bus_msg)
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

        self.mixer.link(self.vt_duck_gain)
        self.vt_duck_gain.link(self.duck_gain)

        # duck_gain feeds one pad on program_fx_mixer; fx_bus_gain will
        # join the OTHER pad when _fx_setup runs a few lines below.
        # The program_fx_mixer's output is what gets tee'd to the
        # remote-DJ monitor return (mix-minus) AND forwarded to
        # master_mixer. Naming the sink var deck_bus_pad so a later
        # reader doesn't confuse it with a duck-related pad.
        deck_bus_pad = self.program_fx_mixer.request_pad_simple("sink_%u")
        self.duck_gain.get_static_pad("src").link(deck_bus_pad)

        program_fx_pad = self.master_mixer.request_pad_simple("sink_%u")
        if self.remote_dj_tee:
            self.program_fx_mixer.get_static_pad("src").link(remote_dj_fixate_caps.get_static_pad("sink"))
            remote_dj_fixate_caps.link(self.remote_dj_tee)
            onair_pad = self.remote_dj_tee.request_pad_simple("src_%u")
            onair_pad.link(program_fx_pad)
        else:
            self.program_fx_mixer.get_static_pad("src").link(program_fx_pad)
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

        # Persistent DJ slot pool (see RemoteDJSlot's docstring for why).
        # Only built when the feature is enabled; otherwise self.dj_slots
        # stays empty and no cycles are spent generating silence for a
        # slot no one will ever occupy.
        if RemoteDJConfig.load().enabled:
            self._build_dj_slots(MAX_DJ_SLOTS)

        # FX bus (one-shot audio carts / hotkeys). Persistent sub-mixer
        # + volume attached via a SINGLE permanent pad on master_mixer.
        # Individual fire chains request pads on the sub-mixer, not on
        # master_mixer, so the main audio path is untouched by fire
        # churn -- the mistake we're deliberately avoiding was the
        # historical RemoteDJ pattern of live master_mixer.request_pad
        # calls at runtime. See FXCart / FXBusConfig docstrings.
        self._fx_setup(convert=None)

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
            .select_related(
                "track", "track__artist", "track__album", "track__category", "track__category__kind",
                # LogItem's own (denormalized) category -- distinct from
                # track__category above -- is read directly by dedication
                # logic (log_item.category.kind.code); select_related it
                # here too or that becomes a surprise lazy query on the
                # engine's single GLib thread.
                "category", "category__kind",
            )
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

    @_glib_safe(default_return=True)
    def _ensure_upcoming_logs(self):
        """No human approval step for now — auto-build (and
        auto-approve) whatever hour needs it: the current hour, so a
        freshly-started or catching-up engine has something to play
        right away, and the next hour once we're within the last
        NEXT_HOUR_LOOKAHEAD_SECONDS of the top of the hour, so a late
        schedule-grid edit still has a chance to take effect before
        it's locked in.

        Never runs build_hour_log synchronously, under any condition --
        deck-idle state alone doesn't prove the engine has no live
        audio or latency-sensitive activity (studio mic, a Remote DJ
        session, FX carts/voice tracks, GStreamer bus/signaling
        callbacks all still need this same single thread serviced
        promptly). All missing-log work goes through _ensure_log_building,
        which builds off-thread and hands the result back via
        GLib.idle_add (see _build_hour_log_worker/_install_built_hour)."""
        if not self.running:
            return False

        close_old_connections()
        now = timezone.localtime()

        with self._lock:
            idle = self.decks["A"] is None and self.decks["B"] is None

        # Kick off (or no-op if already approved/in-flight) BEFORE the
        # monitoring check below, so a first-tick "no log yet" event
        # accurately reports build_in_progress=True rather than False
        # for the instant before the NEXT tick would otherwise have
        # been the first to notice the worker it just started.
        self._ensure_log_building(now.date(), now.hour)

        # Monitoring: distinguish a genuine missed/late rollover (the
        # active log is BEHIND wall-clock time) from two states that
        # must NOT be reported as one: an ordinary mid-hour cold start
        # (no active log at all yet) and an intentional early rollover
        # (the active log is AHEAD of wall-clock time -- _roll_over_to_
        # next_hour deliberately installs the next hour's log a little
        # early to avoid dead air when the current hour's queue runs
        # out first; that's expected station policy, not a warning
        # condition). Ordering (active_key < / == / > now_key), not a
        # plain inequality -- "!=" would also fire for the early-
        # rollover case, which is the opposite of what's intended.
        # Deduplicated per (date, hour) via emit_event's own dedupe_key
        # so a persistent condition doesn't re-alert every 10s tick.
        now_key = (now.date(), now.hour)
        active_key = (self.current_log.date, self.current_log.hour) if self.current_log else None

        if active_key is not None and active_key < now_key:
            if not PlaylistLog.objects.filter(date=now.date(), hour=now.hour, status="approved").exists():
                with self._lock:
                    build_in_progress = now_key in self._building_hours
                emit_event(
                    category="engine", level="warning",
                    title="Current hour's log not ready after rollover",
                    detail={
                        "target_date": str(now.date()), "target_hour": now.hour,
                        "build_in_progress": build_in_progress,
                        "active_log_date": str(self.current_log.date), "active_log_hour": self.current_log.hour,
                    },
                    dedupe_key=f"engine|late-hour-log|{now.date()}|{now.hour}",
                )
        elif active_key is None:
            if not PlaylistLog.objects.filter(date=now.date(), hour=now.hour, status="approved").exists():
                with self._lock:
                    build_in_progress = now_key in self._building_hours
                emit_event(
                    category="engine", level="warning",
                    title="No approved log for current hour",
                    detail={
                        "target_date": str(now.date()), "target_hour": now.hour,
                        "build_in_progress": build_in_progress,
                        "active_log_date": None, "active_log_hour": None,
                    },
                    dedupe_key=f"engine|no-current-log|{now.date()}|{now.hour}",
                )
        # active_key > now_key: intentional early rollover -- not a warning condition.

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
        # for as long as manual mode stayed on. This part is unchanged
        # from before -- _load_log_for is a single indexed query, not
        # the slow build, so it was never the source of the stall.
        if idle and not self.manual_mode:
            self._load_log_for(now.date(), now.hour)
            if self.log_items:
                self._start_next_track()

        seconds_left_in_hour = 3600 - (now.minute * 60 + now.second)
        if seconds_left_in_hour <= NEXT_HOUR_LOOKAHEAD_SECONDS:
            next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            self._ensure_log_building(next_hour.date(), next_hour.hour)
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
        this runs on every 10s tick during the lookahead window.

        Monotonic: never installs a target OLDER than whatever's already
        active. Without this, two async builds racing (e.g. the current
        hour's build and the next hour's build both in flight near a
        boundary) can have the newer one install first and the older,
        slower one install second -- moving the live queue BACKWARD to
        stale content. The advisory lock in build_and_approve_hour_log_
        locked only serializes builders for the SAME (date, hour); it
        does nothing for two different hours racing each other here.
        Forward progress (target > active) is always allowed, including
        installing a next-hour log a little early -- that's the
        intentional early-rollover behavior _roll_over_to_next_hour
        already relies on to avoid dead air, not a bug to guard against."""
        if self.current_log:
            active_key = (self.current_log.date, self.current_log.hour)
            target_key = (target_date, target_hour)
            if target_key == active_key:
                return  # already advanced
            if target_key < active_key:
                print(
                    f"  Ignoring stale completed hour-log build for {target_date} {target_hour:02d}:00 "
                    f"-- active log is already {self.current_log.date} {self.current_log.hour:02d}:00"
                )
                return

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
            .select_related(
                "track", "track__artist", "track__album", "track__category", "track__category__kind",
                "category", "category__kind",  # LogItem's own category, read directly by dedication logic
            )
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

    def _ensure_log_building(self, target_date, target_hour):
        """Kick off an async build for (target_date, target_hour) if no
        approved log exists yet and one isn't already in flight for
        this exact hour in this process. The "approved already exists"
        check here is a cheap fast-path only (true ~59 out of every 60
        minutes in steady state) -- the real, race-free guarantee is
        the advisory lock inside build_and_approve_hour_log_locked,
        which _building_hours doesn't replace, just avoids redundantly
        spawning a thread for.

        A leftover DRAFT does not count as approved -- draft means "not
        committed for on-air" and must never block auto-build, or a
        user who built a preview and forgot to approve/discard it
        silently starves the scheduler at that hour's TOH. (Caught live
        2026-07-22: 1pm draft existed unapproved from an earlier demo,
        engine's advance-to-next-hour couldn't find an approved log at
        12:59, and playback fell back to the previous approved hour
        (12pm news repeated).)"""
        if PlaylistLog.objects.filter(date=target_date, hour=target_hour, status="approved").exists():
            return
        key = (target_date, target_hour)
        with self._lock:
            if key in self._building_hours:
                return
            self._building_hours.add(key)
        threading.Thread(
            target=self._build_hour_log_worker, args=(target_date, target_hour),
            daemon=True,
        ).start()

    def _build_hour_log_worker(self, target_date, target_hour):
        """Runs on a background thread -- touches only the Django ORM,
        never self.log_items/self.current_log/self._queue_cursor or any
        GStreamer object. build_and_approve_hour_log_locked holds a
        Postgres advisory lock across the whole persist-then-approve
        sequence, so a concurrent build from a different process (a
        manual admin rebuild, force_next_hour) can't delete this row out
        from under the approve step. Hands the result back to the main
        thread via GLib.idle_add -- see _install_built_hour -- rather
        than touching engine state directly from this thread."""
        try:
            close_old_connections()
            log, error = build_and_approve_hour_log_locked(target_date, target_hour)
            if error == LOCK_CONTENDED:
                print(f"  Async build deferred for {target_date} {target_hour:02d}:00 -- already being built elsewhere")
                return
            if error:
                print(f"  Auto-build (async) skipped for {target_date} {target_hour:02d}:00 -- {error}")
                emit_event(
                    category="engine", level="warning", title="Async hour-log build failed",
                    detail={"date": str(target_date), "hour": target_hour, "error": error},
                )
                return
            print(f"  Auto-built and approved log (async) for {target_date} {target_hour:02d}:00 ({log.items.count()} items)")
            if self.running:  # don't schedule queue mutations mid-teardown
                GLib.idle_add(self._install_built_hour, target_date, target_hour)
        except Exception as exc:
            print(f"  Async hour-log build crashed (non-fatal): {exc}")
            emit_event(
                category="engine", level="error", title="Async hour-log build crashed",
                detail={
                    "date": str(target_date), "hour": target_hour,
                    "exception": repr(exc), "traceback": traceback.format_exc(),
                },
            )
        finally:
            # Independent cleanup steps -- one raising must not skip the other.
            try:
                connection.close()  # don't leak a per-thread DB connection over a long engine uptime
            finally:
                with self._lock:
                    self._building_hours.discard((target_date, target_hour))

    @_glib_safe(default_return=False)
    def _install_built_hour(self, target_date, target_hour):
        """One-shot GLib.idle_add callback -- runs on the main thread,
        so it's safe to touch engine state here. _advance_to_next_hour_log
        is idempotent AND monotonic -- it installs immediately regardless
        of how late this fires relative to the actual hour boundary
        (intentional early-rollover installs are always allowed, since
        target > active), but silently refuses to move the active queue
        BACKWARD if some other, newer build already installed ahead of
        this one finishing (target < active -- e.g. this hour's own
        build ran unusually long and a next-hour build finished first).
        _advance_to_next_hour_log never itself calls _start_next_track
        (it only swaps the queue), so a genuine cold start needs this
        callback to also perform the idle-recovery check -- otherwise a
        fully built-and-approved log would sit silently unplayed until
        the next 10s tick noticed."""
        if not self.running:
            return False
        self._advance_to_next_hour_log(target_date, target_hour)
        with self._lock:
            idle = self.decks["A"] is None and self.decks["B"] is None
        if idle and not self.manual_mode and self.log_items:
            self._start_next_track()
        return False

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
                .select_related(
                "track", "track__artist", "track__album", "track__category", "track__category__kind",
                "category", "category__kind",  # LogItem's own category, read directly by dedication logic
            )
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
        # Informational, lower severity than the "log not ready" warnings
        # in _ensure_upcoming_logs -- this path fires for two different
        # reasons (ordinary early exhaustion mid-hour, e.g. a DJ skipped
        # ahead of schedule; or the real next hour's async build running
        # long/failing) and this event alone doesn't distinguish them,
        # it's just visibility that the fallback engaged at all.
        emit_event(
            category="engine", level="info", title="Live log extension activated",
            detail={
                "date": str(self.current_log.date), "hour": self.current_log.hour,
                "items_added": len(new_items), "seconds_left_in_hour": round(seconds_left, 1),
            },
        )
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

    def _peek_playable_at_cursor(self):
        """Advance `self._queue_cursor` past any leading unplayable items
        in the current hour's log, then return the item now at the
        cursor (WITHOUT consuming it -- cursor is left pointing AT it).
        Returns None if the current hour has no more playable items.

        Used by the crossfade look-ahead in _poll_position, which needs
        to inspect the upcoming item's track.cue_in_seconds/etc. before
        deciding whether to trigger -- and would AttributeError on a
        deleted-track ghost the same way _next_queue_item would. Kept
        separate from _next_queue_item because the poll path must NOT
        advance the cursor over the item it's about to hand to
        _start_next_track.

        A pending dedication follow-up (self._forced_next_items) must be
        surfaced here FIRST -- this drives crossfade timing/cache
        warming/VT lookahead, and once a follow-up is armed it's what's
        actually going to play next regardless of what self.log_items
        currently contains (which may be a different hour entirely, if
        rollover happened while a dedication intro was on a deck)."""
        for item in self._forced_next_items:
            if _log_item_playable(item)[0]:
                return item
        while self._queue_cursor < len(self.log_items):
            item = self.log_items[self._queue_cursor]
            playable, reason = _log_item_playable(item)
            if playable:
                return item
            print(f"  Skipping log item id={item.id} pos={item.position}: {reason}")
            emit_event(
                category="engine",
                level="warning",
                title=f"Skipped log item at pos {item.position}",
                detail={
                    "log_item_id": item.id,
                    "position": item.position,
                    "reason": reason,
                },
                dedupe_key=f"engine|skip|item={item.id}",
            )
            self._queue_cursor += 1
        return None

    def _next_queue_item(self):
        """Returns (item, is_forced). is_forced is True when item came
        from self._forced_next_items (a dedication follow-up or an
        urgent alert that arrived while one was pending) rather than the
        normal cursor/rollover walk -- the caller (_start_next_track)
        uses this to skip web-request scheduling/dedication-insertion
        for an item that's already been through that decision."""
        # Forced items are checked FIRST, unconditionally -- see
        # _maybe_insert_dedication_intro/_restore_followup_for_intro for
        # why this is what makes a dedication's song survive an hour
        # rollover that happens while the intro is playing.
        while self._forced_next_items:
            item = self._forced_next_items.pop(0)
            playable, reason = _log_item_playable(item)
            if not playable:
                print(f"  Skipping forced log item id={item.id}: {reason}")
                emit_event(
                    category="engine", level="warning", title="Skipped forced log item",
                    detail={"log_item_id": item.id, "reason": reason},
                    dedupe_key=f"engine|skip-forced|item={item.id}",
                )
                continue
            if (
                self.current_log and item.playlist_log_id == self.current_log.id
                and self._queue_cursor < len(self.log_items)
                and self.log_items[self._queue_cursor].id == item.id
            ):
                # Still the same hour, and the normal cursor happens to
                # be sitting on this exact item too -- advance past it
                # so the normal path doesn't hand it out a second time
                # later. If rollover already replaced current_log,
                # there's nothing to adjust -- the new hour's cursor is
                # unrelated to this item.
                self._queue_cursor += 1
            return item, True

        # Walk forward past any unplayable items (track deleted, filepath
        # cleared, file gone from disk) rather than blowing up on
        # log_item.track.filepath below. Each skip is logged so the
        # operator can see in the journal what the auto-approved log
        # tripped over. Rolls over to the next hour's log when the
        # current hour has nothing more to offer.
        while True:
            if self._queue_cursor >= len(self.log_items):
                if not self._roll_over_to_next_hour():
                    return None, False
                continue
            item = self.log_items[self._queue_cursor]
            self._queue_cursor += 1
            playable, reason = _log_item_playable(item)
            if playable:
                return item, False
            print(f"  Skipping log item id={item.id} pos={item.position}: {reason}")
            emit_event(
                category="engine",
                level="warning",
                title=f"Skipped log item at pos {item.position}",
                detail={
                    "log_item_id": item.id,
                    "position": item.position,
                    "reason": reason,
                },
                dedupe_key=f"engine|skip|item={item.id}",
            )

    def _get_upcoming_preview(self):
        """Every remaining item in the current hour's log — however many
        there are, no cap — plus, once those run out, the next hour's
        already-approved items — purely for UI preview (queue table /
        idle-deck 'Up Next'). Read-only: does not touch
        `self._queue_cursor` or `self.current_log`.

        Filters unplayable items (deleted track / missing file) so the
        state-writer downstream can't AttributeError on a NULLed track
        FK -- _write_state runs inside the position-poll callback, and
        an uncaught exception there hangs the whole engine (see
        _glib_safe). The playback path skips these items independently
        (_next_queue_item), so filtering them from the UI preview keeps
        the two views consistent.

        Forced items (a pending dedication follow-up) are prepended
        first, deduplicated against whatever's also still sitting in
        self.log_items -- this is what engine_state.json's "queue"
        reflects, which the web-request reconciliation command reads to
        decide deck/queue membership. Without this, a protected
        follow-up song would look STRANDED to that reconciliation the
        moment rollover swaps self.log_items to a different hour, even
        though it's guaranteed to actually play next."""
        forced = [it for it in self._forced_next_items if _log_item_playable(it)[0]]
        forced_ids = {it.id for it in forced}
        items = list(self.log_items[self._queue_cursor:])
        if not items:
            peek = self._peek_next_hour()
            if peek:
                items = list(peek[1])
        return forced + [it for it in items if it.id not in forced_ids and _log_item_playable(it)[0]]

    def _start_next_track(self, slot=None):
        """Load the next queued item into `slot` (or whichever slot is
        free, preferring A) and start it playing right away."""
        if slot is None:
            slot = self._free_slot()
        if slot is None:
            return

        log_item, is_forced = self._next_queue_item()
        if log_item is None:
            self._on_log_exhausted(slot)
            return

        # Skip request-scheduling AND dedication-splicing for a forced
        # item (already-committed intro/follow-up pair placed by an
        # earlier splice, or an urgent alert) and on the resume path --
        # the LogItem about to be handed to _create_deck is one
        # _apply_resume_hint_queue_rewind already rewound the cursor to,
        # meant to CONTINUE from a mid-play position -- not an "open
        # request slot." A swap here would replace log_item.track with a
        # different track, which then wouldn't match hint["track_id"] in
        # _create_deck, silently disable the auto-resume seek, and leave
        # the UI cursor at the resumed position with audio starting from
        # zero. Guard is on _resume_hint (not just first-track-after-
        # restart) so it holds until _create_deck actually consumes and
        # clears it.
        if not is_forced and not getattr(self, "_resume_hint", None):
            result = maybe_schedule_song_request(log_item)
            if result is SCHEDULING_CONTENDED:
                # Another transaction is mid-write to this exact
                # LogItem and didn't resolve within the bounded wait --
                # its data can't be trusted either way right now. Don't
                # guess: skip this item entirely rather than risk
                # playing a stale track while the database ends up
                # showing something else. played_at stays NULL, same
                # as any other unplayed item; whatever was being
                # scheduled into it resolves on its own once that other
                # transaction commits.
                self._start_next_track(slot=slot)
                return
            log_item = result or log_item
            log_item = self._maybe_insert_dedication_intro(log_item)

        if log_item.category_id and log_item.category.code == "Dedications":
            # Whether reached via a fresh splice (log_item is the intro
            # _maybe_insert_dedication_intro just returned) or via the
            # ordinary cursor walk / forced list after a restart, never
            # air a Dedications item without positively re-confirming its
            # paired song first -- synchronous recursive call so the GLib
            # loop never regains control in the gap, same reasoning as
            # the SCHEDULING_CONTENDED skip above (rollover cannot
            # intervene mid-call).
            if not self._restore_followup_for_intro(log_item):
                print(f"  Skipping unpaired dedication intro log_item={log_item.id}")
                self._start_next_track(slot=slot)
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
        # Belt-and-braces: _next_queue_item already filters unplayable
        # items, but _create_deck is also reached from other paths
        # (_insert_urgent_next, crossfade swap, log reload flow). One
        # centralized check here catches all of them uniformly instead
        # of relying on every caller to remember.
        playable, reason = _log_item_playable(log_item)
        if not playable:
            item_id = log_item.id if log_item is not None else "None"
            print(f"  Cannot create deck for log item id={item_id}: {reason}")
            return None
        track = log_item.track
        filepath = track.filepath

        self._write_now_playing(track)
        self._write_rbds_category_state(track)

        # Auto-resume: if the previous engine instance was mid-play on
        # the same track (see _read_resume_hint), seek the fresh deck
        # to that position after creation. Hint is consumed once --
        # clearing on match OR mismatch so future deck loads follow
        # their normal path. Unlike the pause-resume path (which sets
        # resume_position_ns to skip silence prime), auto-resume keeps
        # the silence prime intact and just seeks after the deck is
        # linked -- matches what happens when we manually issue a seek
        # from the /api/engine/seek/ endpoint against a running deck.
        _auto_resume_position_ns = None
        if resume_position_ns is None and getattr(self, "_resume_hint", None):
            hint = self._resume_hint
            if hint["track_id"] == track.id:
                _auto_resume_position_ns = int(hint["position"] * Gst.SECOND)
                print(f"  Auto-resuming deck [{slot}] at {hint['position']:.1f}s (track match)")
            self._resume_hint = None

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
            aired_at = timezone.now()
            played_at_written = False
            try:
                close_old_connections()
                log_item.played_at = aired_at
                log_item.save(update_fields=["played_at"])
                played_at_written = True
                Track.objects.filter(id=track.id).update(
                    last_played_at=aired_at,
                    play_count=track.play_count + 1,
                )
            except Exception as exc:
                print(f"  DB write failed (non-fatal): {exc}")
            # Web Requests fulfillment is gated on played_at ITSELF
            # having succeeded, independent of whether the Track
            # counter update above (a separate statement, same
            # try/except) also succeeded -- must not mark a request
            # fulfilled when played_at never actually saved, and must
            # not skip marking it fulfilled just because an unrelated
            # counter update happened to fail.
            if played_at_written:
                try:
                    mark_song_requests_aired(log_item, aired_at)
                except Exception as exc:
                    print(f"  Web request air-time update failed (non-fatal): {exc}")
            # Append-only PlayEvent ledger for royalty / SoundExchange
            # reporting -- distinct from LogItem.played_at because it
            # snapshots ISRC / album / label / category_kind that a
            # future Track edit or LogItem prune could otherwise wipe.
            # Best-effort: any DB failure here is logged and dropped --
            # missing a PlayEvent row for a single spin is preferable to
            # failing the deck-creation path and dropping the track.
            #
            # Dedications-category plays are excluded entirely -- a
            # spoken intro isn't a performance of a recording, and
            # counting it would double-count a spin (the requested song
            # right behind it gets its own PlayEvent normally).
            # LogItem.played_at is still written above regardless,
            # unaffected -- this exclusion only skips the PlayEvent row.
            is_dedication_play = bool(
                log_item.category_id and log_item.category and log_item.category.code == "Dedications"
            )
            if not is_dedication_play:
                try:
                    close_old_connections()
                    # position >= 9999 is api_engine_insert_track's marker
                    # for a manual / remote-DJ insert. Playlist-play-now
                    # rebuilds a whole PlaylistLog and looks like a normal
                    # scheduled hour from here, so it also reads as
                    # "scheduled" -- fine for royalty reporting (SoundExchange
                    # doesn't distinguish), and if we later want the split we
                    # can add a `source` field to LogItem itself.
                    pe_source = "insert" if getattr(log_item, "position", 0) >= 9999 else "scheduled"
                    pe_category_kind = ""
                    if log_item.category_id and log_item.category and log_item.category.kind_id:
                        pe_category_kind = log_item.category.kind.name
                    pe = PlayEvent.objects.create(
                        track=track,
                        track_title=track.title or "",
                        track_artist=(track.artist.name if track.artist else ""),
                        album_title=(track.album.title if track.album else ""),
                        record_label=track.record_label or "",
                        isrc=getattr(track, "isrc", "") or "",
                        category_kind=pe_category_kind,
                        source=pe_source,
                        started_at=timezone.now(),
                    )
                    deck.play_event_id = pe.id
                except Exception as exc:
                    print(f"  PlayEvent write failed (non-fatal): {exc}")
            print(f"  [{slot}] Playing: {track.artist.name if track.artist else '?'} - {track.title}")
        else:
            print(f"  [{slot}] Resumed: {track.artist.name if track.artist else '?'} - {track.title} at {start_offset:.1f}s")

        # Auto-resume seek: fresh deck was just linked into the mixer,
        # do the actual seek now that the pipeline can accept it. Uses
        # FLUSH so any pre-decoded buffers get dropped -- otherwise we'd
        # hear a tiny chunk from position 0 before the seek lands.
        if _auto_resume_position_ns is not None:
            try:
                deck.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    _auto_resume_position_ns,
                )
                # started_at drives _get_deck_position; adjust it so
                # UI position readouts line up with the audio.
                deck.started_at = time.time() - (_auto_resume_position_ns / Gst.SECOND)
            except Exception as exc:
                print(f"  Auto-resume seek failed (non-fatal): {exc}")

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

        # Same _log_item_playable guard as the real deck creator -- a
        # NULLed track FK or a vanished file would otherwise throw right
        # here inside the position-poll callback path.
        playable, _reason = _log_item_playable(log_item)
        if not playable:
            return
        filepath = log_item.track.filepath
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

    @_glib_safe(default_return=False)
    def _teardown_cache_warmer(self, warm_pipeline):
        warm_pipeline.set_state(Gst.State.NULL)
        return False  # one-shot GLib timeout, don't repeat

    def _remove_deck(self, deck):
        # Close out the PlayEvent row (if one was written at
        # _create_deck) BEFORE we drop the lock -- ended_at and
        # duration_played_seconds are set from the row's own
        # started_at, so this is safe against clock skew. Best-effort;
        # a DB failure here is logged and dropped, same policy as the
        # create-side write. Report-time query drops rows whose
        # duration is below the SoundExchange 30s threshold, so a
        # spurious short close-out here still gets filtered on export.
        if deck.play_event_id:
            try:
                close_old_connections()
                now = timezone.now()
                pe = PlayEvent.objects.filter(id=deck.play_event_id).only(
                    "id", "started_at"
                ).first()
                if pe is not None:
                    duration = None
                    if pe.started_at:
                        duration = max(0.0, (now - pe.started_at).total_seconds())
                    PlayEvent.objects.filter(id=pe.id).update(
                        ended_at=now,
                        duration_played_seconds=duration,
                    )
            except Exception as exc:
                print(f"  PlayEvent close-out failed (non-fatal): {exc}")

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

    def _read_resume_hint(self):
        """Look for a bookmark file left by the PREVIOUS engine instance
        (STATE_PATH survives a service restart because it lives on the
        tmpfs /run, which is only cleared on reboot). If it's fresh
        (< 90s old), record the (track_id, position, log_item_id) of
        whatever was playing so the FIRST matching deck load in
        _create_deck can seek back to that spot -- turning an engine
        restart mid-song into a ~1-2s dead-air gap instead of a "start
        next track from scratch" jump.

        Only bookmarks positions > 5s (a track that JUST started
        doesn't need a seek).

        Prefers the deck with the OLDEST-known log_item_id when both
        slots are populated (a mid-crossfade snapshot). Rationale: the
        queue cursor advances the moment a new deck is created, so at
        any mid-crossfade moment the cursor is already past whatever
        deck A is on -- if we pick deck B's hint, the next
        _load_current_hour_log will resume-at-cursor which is already
        past deck A's LogItem, and the next _start_next_track will
        load deck B's item, and deck B's audio (which is what the
        listener actually hears LATER in the crossfade) resumes
        correctly. But the outgoing (deck A) audio gets restarted from
        zero as it re-enters as the next-item after cursor advance --
        same on-air artefact the 09:48 restart produced (Cannons
        loaded from 0 while Glen Campbell's remaining ~4 minutes were
        skipped).

        Fix: pick the deck whose log_item_id is OLDEST in queue order
        (i.e. the outgoing deck), and later in _create_deck we rewind
        the queue cursor to that item so IT loads first. Deck A hint
        is preferred; if only B is populated we take that instead.

        A Dedications-category deck is ALWAYS a candidate regardless of
        the 5s position floor, and always sorts first regardless of the
        oldest-log_item-id tiebreak above -- a spoken intro is only a
        few seconds long, so it would almost never clear the floor, and
        even when it did, would lose the crossfade tiebreak to the
        (older-log_item-id) outgoing music track every time. Without
        this, a crash while a dedication intro was on-air would
        essentially never produce a correct resume hint, silently
        defeating _restore_dedication_sequence_from_resume_hint below."""
        self._resume_hint = None
        try:
            if not STATE_PATH.is_file():
                return
            age = time.time() - STATE_PATH.stat().st_mtime
            if age > 90:
                return
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            # Collect candidates from both slots.
            candidates = []
            for slot in ("A", "B"):
                deck = (data.get("decks") or {}).get(slot)
                if not deck or not deck.get("track_id"):
                    continue
                is_dedication = deck.get("category") == "Dedications"
                position = float(deck.get("position", 0))
                if position > 5 or is_dedication:
                    candidates.append({
                        "slot": slot,
                        "track_id": deck["track_id"],
                        "position": position,
                        "log_item_id": deck.get("log_item_id"),
                        "is_dedication": is_dedication,
                    })
            if not candidates:
                return
            # Dedications sort first regardless of age; otherwise, sort
            # by log_item_id ascending so the OLDEST (outgoing) LogItem
            # wins when a mid-crossfade snapshot has both slots
            # populated. Missing log_item_id sinks to the end (older
            # state files without the field still match on track_id at
            # deck creation time -- pre-fix behavior).
            candidates.sort(key=lambda c: (not c["is_dedication"], c["log_item_id"] is None, c["log_item_id"] or 0))
            hint = candidates[0]
            self._resume_hint = {
                "track_id": hint["track_id"],
                "position": hint["position"],
                "log_item_id": hint["log_item_id"],
            }
            print(
                f"  Resume hint: track {hint['track_id']} at {hint['position']:.1f}s "
                f"(log_item {hint['log_item_id']}, slot {hint['slot']}, age {age:.1f}s)"
            )
        except Exception as exc:
            print(f"  Resume hint read failed (non-fatal): {exc}")

    def _apply_resume_hint_queue_rewind(self):
        """Called AFTER _load_current_hour_log. If we have a resume
        hint carrying a log_item_id, walk the queue and back the
        cursor up to that item so it loads first when
        _start_next_track fires. If the hint's LogItem isn't in the
        current-hour queue (e.g. the log was regenerated across the
        restart), leave the cursor alone and let the normal flow
        proceed; the resume-hint's track_id match in _create_deck
        won't fire, and playback starts fresh -- same as pre-fix."""
        hint = getattr(self, "_resume_hint", None)
        if not hint or not hint.get("log_item_id"):
            return
        if not hasattr(self, "log_items") or not self.log_items:
            return
        target = hint["log_item_id"]
        for idx, item in enumerate(self.log_items):
            if item.id == target:
                # Only rewind (never advance) -- the cursor may already
                # be at target from _load_current_hour_log's own
                # resume-at logic; leave it alone in that case.
                if getattr(self, "_queue_cursor", 0) > idx:
                    print(f"  Resume queue cursor: rewinding {self._queue_cursor} -> {idx}")
                    self._queue_cursor = idx
                return

    # ------------------------------------------------------------------
    # FX bus (one-shot audio carts / hotkeys)
    # ------------------------------------------------------------------
    def _fx_setup(self, convert):
        """Build the persistent FX sub-mixer and attach it to master_mixer
        via one permanent pad. Called during _build_main_pipeline. Fires
        (individual one-shot chains) are added/removed against
        self.fx_submix at runtime; this method never touches master_mixer
        past its first request_pad. self._fx_fires is the live-fires
        registry: {fire_id: {cart_id, pipeline, pad, ...}}."""
        self.fx_submix = Gst.ElementFactory.make("audiomixer", "fx_submix")
        self.fx_bus_gain = Gst.ElementFactory.make("volume", "fx_bus_gain")

        try:
            cfg = FXBusConfig.load()
            self.fx_bus_gain.set_property("volume", 10 ** (cfg.volume_db / 20.0))
        except Exception as exc:
            print(f"  FXBusConfig read failed at pipeline build: {exc}")
            self.fx_bus_gain.set_property("volume", 1.0)

        self.main_pipeline.add(self.fx_submix)
        self.main_pipeline.add(self.fx_bus_gain)

        self.fx_submix.link(self.fx_bus_gain)
        # FX bus joins the program_fx_mixer, NOT master_mixer directly.
        # See _build_main_pipeline's program_fx_mixer comment for the
        # reason -- FX has to sit UPSTREAM of the remote-DJ tee to be
        # audible in the remote DJ's monitor return. program_fx_mixer's
        # output feeds either the remote_dj_tee (when configured) or
        # master_mixer directly.
        fx_pad = self.program_fx_mixer.request_pad_simple("sink_%u")
        self.fx_bus_gain.get_static_pad("src").link(fx_pad)

        # Permanent silence source into fx_submix. Without this, the
        # FX sub-mixer has no upstream data when no fire is active,
        # and the downstream chain (master_mixer -> alsasink) drifts
        # into a cold state -- the first fire after idle then loses
        # its leading edge while the path spins back up. Live silence
        # keeps the mixer/sink continuously producing buffers so a
        # fresh fire's audio hits a hot path from buffer 0. Same
        # pattern the RemoteDJ persistent subchain and the deck
        # silence prime already use for the same underlying reason.
        fx_silence_src = Gst.ElementFactory.make("audiotestsrc", "fx_silence_src")
        fx_silence_src.set_property("wave", "silence")
        # is-live tells downstream this is a real-time source producing
        # per-clock-tick, so the mixer treats its timestamps as "now"
        # instead of racing them.
        fx_silence_src.set_property("is-live", True)
        fx_silence_caps = Gst.ElementFactory.make("capsfilter", "fx_silence_caps")
        fx_silence_caps.set_property("caps", Gst.Caps.from_string(
            f"audio/x-raw,rate={self.pipeline_sample_rate},channels=2"
        ))
        self.main_pipeline.add(fx_silence_src)
        self.main_pipeline.add(fx_silence_caps)
        fx_silence_src.link(fx_silence_caps)
        silence_pad = self.fx_submix.request_pad_simple("sink_%u")
        fx_silence_caps.get_static_pad("src").link(silence_pad)
        # Retain refs so nothing gets GC'd; never released.
        self._fx_silence_src = fx_silence_src
        self._fx_silence_caps = fx_silence_caps
        self._fx_silence_pad = silence_pad

        self._fx_fires = {}   # fire_id -> {cart_id, filesrc, decodebin, convert, resample, pad, started_at, duration}
        self._fx_next_id = 1
        self._fx_lock = threading.Lock()

        # VT state machine. See _vt_maybe_enter for the shape of the
        # dict when a VT sequence is active. 'idle' means we're in the
        # normal crossfade regime; everything else means the sequence
        # is mid-flight and the normal next-track trigger should stay
        # suppressed.
        self._vt = {"phase": "idle"}
        self._vt_lock = threading.Lock()

    def _fx_apply_volume(self):
        """Refreshed by the reload_fx_config command -- reads FXBusConfig
        and updates the fx_bus_gain volume live. Volume changes take
        effect on the next audio sample without any element churn."""
        try:
            cfg = FXBusConfig.load()
            self.fx_bus_gain.set_property("volume", 10 ** (cfg.volume_db / 20.0))
        except Exception as exc:
            print(f"  FX volume reload failed: {exc}")

    def _fx_active_count(self):
        with self._fx_lock:
            return len(self._fx_fires)

    def _fx_fires_for_cart(self, cart_id):
        with self._fx_lock:
            return [fid for fid, f in self._fx_fires.items() if f["cart_id"] == cart_id]

    def _fx_fire(self, cart_id):
        """Play one FXCart. Honors retrigger_mode + polyphony cap. Returns
        True if a new fire was started, False otherwise (dropped by cap
        or ignored by retrigger)."""
        try:
            close_old_connections()
            cart = FXCart.objects.filter(id=cart_id, enabled=True).first()
        except Exception as exc:
            print(f"  fx_fire: DB read failed for cart {cart_id}: {exc}")
            return False
        if cart is None:
            print(f"  fx_fire: cart {cart_id} not found or disabled")
            return False
        if not cart.filepath or not Path(cart.filepath).is_file():
            print(f"  fx_fire: cart {cart_id} file missing at {cart.filepath!r}")
            return False

        # Retrigger handling. 'restart' stops any existing fire for this
        # cart before starting fresh; 'ignore' drops the second click;
        # 'stop' halts the current fire and returns without starting a
        # new one -- makes the button a click-to-play, click-to-stop
        # toggle.
        existing = self._fx_fires_for_cart(cart_id)
        mode = cart.retrigger_mode
        if existing:
            if mode == "ignore":
                return False
            for fire_id in existing:
                self._fx_stop(fire_id)
            if mode == "stop":
                return False
            # 'restart' -> fall through to start a new fire below

        # Polyphony cap. After honoring retrigger (which may have freed
        # slots via stop) check the live count. A 5th simultaneous fire
        # from different carts gets dropped rather than kicking any
        # existing one -- less surprising to the operator.
        try:
            cap = FXBusConfig.load().polyphony_cap
        except Exception:
            cap = 4
        if self._fx_active_count() >= cap:
            print(f"  fx_fire: polyphony cap ({cap}) hit; dropping cart {cart_id}")
            return False

        # Build the fire chain: filesrc -> decodebin -> audioconvert ->
        # audioresample -> per-cart gain -> fx_submix pad. Same shape as
        # _create_deck but scoped to the FX sub-mixer -- pad churn stays
        # confined here and can't cross-affect the main audio chain.
        with self._fx_lock:
            fire_id = self._fx_next_id
            self._fx_next_id += 1

        filesrc = Gst.ElementFactory.make("filesrc", f"fx_filesrc_{fire_id}")
        decodebin = Gst.ElementFactory.make("decodebin", f"fx_decodebin_{fire_id}")
        convert = Gst.ElementFactory.make("audioconvert", f"fx_convert_{fire_id}")
        resample = Gst.ElementFactory.make("audioresample", f"fx_resample_{fire_id}")
        # Force pipeline_sample_rate + stereo before joining the fx_submix.
        # Without this capsfilter, audiomixer sees mismatched input caps
        # (a mono 22050 Hz drop against a stereo 44100 Hz mixer output)
        # and silently drops the input -- confirmed as the reason cart
        # fires produced no audio during the first live test. Same pattern
        # decks use to force stereo output before the master mixer.
        fx_caps = Gst.ElementFactory.make("capsfilter", f"fx_caps_{fire_id}")
        fx_caps.set_property("caps", Gst.Caps.from_string(
            f"audio/x-raw,rate={self.pipeline_sample_rate},channels=2"
        ))
        gain = Gst.ElementFactory.make("volume", f"fx_gain_{fire_id}")
        filesrc.set_property("location", cart.filepath)
        gain.set_property("volume", 10 ** ((cart.gain_db or 0.0) / 20.0))

        for el in (filesrc, decodebin, convert, resample, fx_caps, gain):
            self.main_pipeline.add(el)

        filesrc.link(decodebin)
        convert.link(resample)
        resample.link(fx_caps)
        fx_caps.link(gain)

        fx_pad = self.fx_submix.request_pad_simple("sink_%u")
        gain.get_static_pad("src").link(fx_pad)

        # Running-time offset: the FX submixer has been alive since
        # engine start (running for minutes/hours). A brand-new file
        # source's buffers are timestamped starting at 0, which the
        # mixer sees as "arrived way in the past" and drops entirely.
        # Same fix _apply_pad_offset uses for decks -- set the src
        # pad's offset to the pipeline's current running time so the
        # mixer sees the buffers as "now" instead of ancient.
        clock = self.main_pipeline.get_clock()
        if clock:
            running_time = clock.get_time() - self.main_pipeline.get_base_time()
            gain.get_static_pad("src").set_offset(running_time)

        state = {
            "cart_id": cart_id,
            "filesrc": filesrc, "decodebin": decodebin,
            "convert": convert, "resample": resample,
            "fx_caps": fx_caps, "gain": gain,
            "fx_pad": fx_pad,
            "started_at": time.time(),
        }
        with self._fx_lock:
            self._fx_fires[fire_id] = state

        # decodebin's src pad only exists once it's had a chance to
        # sniff the file. Wire it into convert here rather than
        # pre-linking (which would fail with "no matching template").
        # Filter for audio pads specifically -- matches the deck
        # pattern; a video-carrying container (mp4 with cover art)
        # would otherwise produce a non-audio pad we'd wrongly link.
        def _on_decodebin_pad_added(decodebin_el, new_pad, fire_id=fire_id):
            caps = new_pad.get_current_caps() or new_pad.query_caps(None)
            if caps and caps.get_size():
                struct_name = caps.get_structure(0).get_name()
                if not struct_name.startswith("audio"):
                    return
            convert_sink = convert.get_static_pad("sink")
            if convert_sink.is_linked():
                return
            new_pad.link(convert_sink)
            print(f"  FX fire {fire_id}: decodebin linked ({struct_name if caps else 'unknown caps'})")

        decodebin.connect("pad-added", _on_decodebin_pad_added)

        # Wire an EOS handler by watching the bus for messages from THIS
        # fire's elements. The bus is shared, so we filter by src.
        bus = self.main_pipeline.get_bus()
        bus.add_signal_watch()   # idempotent -- safe to call again
        bus.connect("message::eos", self._fx_on_eos, fire_id)

        # State: PLAYING. Same order as _create_deck's sync_state pattern.
        for el in (filesrc, decodebin, convert, resample, fx_caps, gain):
            el.sync_state_with_parent()

        print(f"  FX fire {fire_id}: cart {cart_id} ({cart.name!r})")
        return True

    def _fx_on_eos(self, bus, message, fire_id):
        with self._fx_lock:
            state = self._fx_fires.get(fire_id)
        if state is None:
            return
        # EOS from any element on the main pipeline reaches this handler;
        # filter by checking the src is one of ours.
        src = message.src
        our_srcs = (state["filesrc"], state["decodebin"], state["convert"],
                    state["resample"], state["gain"])
        if src not in our_srcs:
            return

        # If this was a VT fire, advance the VT state machine BEFORE
        # tearing down (the state read needs the fire_id to still be
        # in _fx_fires so _vt_handle_outgoing_ended can see the
        # in-flight fire correctly).
        vt_kind = state.get("vt_kind")
        if vt_kind:
            self._vt_on_fire_eos(vt_kind, fire_id)

        self._fx_stop(fire_id)

    def _fx_stop(self, fire_id):
        """Teardown a fire's chain and release its sub-mixer pad. Safe to
        call for a stale fire_id (silent no-op). Called by both EOS and
        retrigger-mode-stop paths."""
        with self._fx_lock:
            state = self._fx_fires.pop(fire_id, None)
        if state is None:
            return
        try:
            for el in (state["filesrc"], state["decodebin"], state["convert"],
                        state["resample"], state["fx_caps"], state["gain"]):
                el.set_state(Gst.State.NULL)
            try:
                state["gain"].get_static_pad("src").unlink(state["fx_pad"])
            except Exception:
                pass
            self.fx_submix.release_request_pad(state["fx_pad"])
            for el in (state["gain"], state["fx_caps"], state["resample"],
                        state["convert"], state["decodebin"], state["filesrc"]):
                self.main_pipeline.remove(el)
        except Exception as exc:
            print(f"  FX teardown fire {fire_id} failed: {exc}")

    # ------------------------------------------------------------------
    # VT state machine (voice-track sequenced transition)
    # ------------------------------------------------------------------
    def _vt_ramp_duck_to_db(self, target_db, duration_ms):
        """Linear ramp vt_duck_gain to the target dB value over
        duration_ms. Simple stepper on GLib.timeout_add -- 20ms per
        step, so a 300ms ramp uses 15 steps. Cheap enough not to
        matter versus the audio interrupt rate."""
        if getattr(self, "vt_duck_gain", None) is None:
            return
        start_vol = self.vt_duck_gain.get_property("volume")
        if target_db is None or target_db == 0.0:
            target_vol = 1.0
        else:
            target_vol = 10 ** (target_db / 20.0)
        steps = max(1, int(duration_ms / 20))
        step_dur = max(1, int(duration_ms / steps))
        step_delta = (target_vol - start_vol) / steps
        step_counter = [0]

        def _do_step():
            step_counter[0] += 1
            new_vol = max(0.0, start_vol + step_delta * step_counter[0])
            self.vt_duck_gain.set_property("volume", new_vol)
            return step_counter[0] < steps
        GLib.timeout_add(step_dur, _do_step)

    def _vt_fire_file(self, filepath, gain_db, kind):
        """Fire an audio file through the FX submix bus. Same pipeline
        pattern as _fx_fire but takes a filepath directly (VTs don't
        have cart_ids) and tags the fire state with vt_kind so the
        EOS dispatcher can advance the VT state machine. Returns
        fire_id (or None on failure)."""
        if not Path(filepath).is_file():
            print(f"  VT fire: file missing {filepath!r}")
            return None
        with self._fx_lock:
            fire_id = self._fx_next_id
            self._fx_next_id += 1

        filesrc = Gst.ElementFactory.make("filesrc", f"vt_filesrc_{fire_id}")
        decodebin = Gst.ElementFactory.make("decodebin", f"vt_decodebin_{fire_id}")
        convert = Gst.ElementFactory.make("audioconvert", f"vt_convert_{fire_id}")
        resample = Gst.ElementFactory.make("audioresample", f"vt_resample_{fire_id}")
        vt_caps = Gst.ElementFactory.make("capsfilter", f"vt_caps_{fire_id}")
        vt_caps.set_property("caps", Gst.Caps.from_string(
            f"audio/x-raw,rate={self.pipeline_sample_rate},channels=2"
        ))
        gain = Gst.ElementFactory.make("volume", f"vt_gain_{fire_id}")
        filesrc.set_property("location", filepath)
        gain.set_property("volume", 10 ** ((gain_db or 0.0) / 20.0))

        for el in (filesrc, decodebin, convert, resample, vt_caps, gain):
            self.main_pipeline.add(el)
        filesrc.link(decodebin)
        convert.link(resample)
        resample.link(vt_caps)
        vt_caps.link(gain)
        fx_pad = self.fx_submix.request_pad_simple("sink_%u")
        gain.get_static_pad("src").link(fx_pad)
        clock = self.main_pipeline.get_clock()
        if clock:
            running_time = clock.get_time() - self.main_pipeline.get_base_time()
            gain.get_static_pad("src").set_offset(running_time)

        state = {
            "cart_id": None,
            "vt_kind": kind,   # 'outro' | 'intro' -- key for dispatch
            "filesrc": filesrc, "decodebin": decodebin,
            "convert": convert, "resample": resample,
            "fx_caps": vt_caps, "gain": gain, "fx_pad": fx_pad,
            "started_at": time.time(),
        }
        with self._fx_lock:
            self._fx_fires[fire_id] = state

        def _on_pad_added(el, new_pad, fire_id=fire_id):
            caps = new_pad.get_current_caps() or new_pad.query_caps(None)
            if caps and caps.get_size():
                if not caps.get_structure(0).get_name().startswith("audio"):
                    return
            convert_sink = convert.get_static_pad("sink")
            if convert_sink.is_linked():
                return
            new_pad.link(convert_sink)
        decodebin.connect("pad-added", _on_pad_added)

        bus = self.main_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._fx_on_eos, fire_id)
        for el in (filesrc, decodebin, convert, resample, vt_caps, gain):
            el.sync_state_with_parent()

        print(f"  VT fire {fire_id} ({kind}): {Path(filepath).name}")
        return fire_id

    def _vt_maybe_enter(self, outgoing_deck):
        """Called from _poll_position when outgoing hits outro_starts.
        Looks up VTs for the outgoing and incoming tracks; if either
        exists, enters VT mode, suppresses the normal next-track
        trigger, fires the outro-VT if one exists, and ramps the duck
        attack. If NO VTs exist, this is a no-op and the caller
        continues into the normal crossfade path."""
        try:
            close_old_connections()
            outgoing_vt = VoiceTrack.objects.filter(
                track=outgoing_deck.track, position="outro",
            ).first()
            next_item = self._peek_playable_at_cursor() if hasattr(self, "_peek_playable_at_cursor") else None
            incoming_vt = None
            incoming_track = None
            if next_item is not None:
                incoming_track = next_item.track
                incoming_vt = VoiceTrack.objects.filter(
                    track=incoming_track, position="intro",
                ).first()
                # A pending dedication supersedes the requested song's own
                # incoming intro VT for this specific play -- the outgoing
                # track's own outro VT, if any, is unaffected and still
                # fires below as normal. Without this, a freshly-spliced
                # dedication intro and a configured intro VT cart could
                # both start at once, talking over each other.
                has_dedication = SongRequest.objects.filter(
                    status="scheduled", log_item_id=next_item.id,
                    intro_track__isnull=False, intro_track__ready2air=True, intro_log_item__isnull=True,
                ).exists()
                if has_dedication:
                    incoming_vt = None
        except Exception as exc:
            print(f"  VT lookup failed: {exc}")
            return False

        if outgoing_vt is None and incoming_vt is None:
            return False   # no VTs; fall through to normal crossfade

        cfg = VoiceTrackConfig.load()
        with self._vt_lock:
            self._vt = {
                "phase": "outro_playing",
                "outgoing_track_id": outgoing_deck.track.id,
                "outgoing_vt_filepath": outgoing_vt.filepath if outgoing_vt else None,
                "outgoing_vt_gain": outgoing_vt.gain_db if outgoing_vt else 0.0,
                "outgoing_vt_fire_id": None,
                "outgoing_ended": False,
                "incoming_track_id": incoming_track.id if incoming_track else None,
                "incoming_track": incoming_track,
                "incoming_vt_filepath": incoming_vt.filepath if incoming_vt else None,
                "incoming_vt_gain": incoming_vt.gain_db if incoming_vt else 0.0,
                "incoming_vt_duration": incoming_vt.duration_seconds if incoming_vt else 0.0,
                "incoming_vt_fire_id": None,
                "incoming_intro_until": (incoming_track.intro_until_seconds or 0.0) if incoming_track else 0.0,
                "incoming_started": False,
            }
        # Suppress normal next-track trigger for the remainder of this
        # transition; _vt_start_incoming_now flips it back and calls
        # _start_next_track explicitly at the right moment.
        self._next_triggered = True

        print(f"  VT enter: outgoing track_id={outgoing_deck.track.id} "
                f"outro_vt={'yes' if outgoing_vt else 'no'} "
                f"intro_vt={'yes' if incoming_vt else 'no'}")

        self._vt_ramp_duck_to_db(cfg.program_duck_db, cfg.duck_ramp_ms)

        if outgoing_vt is not None:
            fire_id = self._vt_fire_file(
                outgoing_vt.filepath, outgoing_vt.gain_db, "outro",
            )
            with self._vt_lock:
                self._vt["outgoing_vt_fire_id"] = fire_id
        # else: state stays outro_playing; outgoing deck end will
        # trigger the transition to inter_gap without firing anything.

        return True

    def _vt_on_fire_eos(self, kind, fire_id):
        """Called from _fx_on_eos when a VT fire ends. Advances the
        state machine according to which VT (outro or intro) just
        finished."""
        with self._vt_lock:
            phase = self._vt.get("phase", "idle")

        if kind == "outro":
            with self._vt_lock:
                self._vt["outgoing_vt_fire_id"] = None
            if phase == "outro_tail":
                # Outgoing already ended and we were waiting on outro-VT
                self._vt_advance_to_intro_gap()
            # else: still 'outro_playing' -- outgoing deck end will drive
            # the next transition. If outgoing ends and outro-VT already
            # finished, _handle_deck_finished's VT branch will advance
            # immediately.
        elif kind == "intro":
            with self._vt_lock:
                self._vt["incoming_vt_fire_id"] = None
            self._vt_complete()

    def _vt_handle_outgoing_ended(self):
        """Called from _handle_deck_finished when the outgoing deck
        ends while we're in VT mode. Advances to outro_tail if the
        outro-VT is still going, else straight to inter_gap."""
        with self._vt_lock:
            self._vt["outgoing_ended"] = True
            outro_fire_id = self._vt.get("outgoing_vt_fire_id")
        outro_still_going = False
        if outro_fire_id is not None:
            with self._fx_lock:
                outro_still_going = outro_fire_id in self._fx_fires
        if outro_still_going:
            with self._vt_lock:
                self._vt["phase"] = "outro_tail"
        else:
            self._vt_advance_to_intro_gap()

    def _vt_advance_to_intro_gap(self):
        with self._vt_lock:
            self._vt["phase"] = "inter_gap"
        cfg = VoiceTrackConfig.load()
        GLib.timeout_add(max(1, cfg.min_gap_ms), self._vt_start_intro_phase)

    def _vt_start_intro_phase(self):
        with self._vt_lock:
            filepath = self._vt.get("incoming_vt_filepath")
            gain_db = self._vt.get("incoming_vt_gain") or 0.0
            vt_duration = self._vt.get("incoming_vt_duration") or 0.0
            intro_until = self._vt.get("incoming_intro_until") or 0.0
            self._vt["phase"] = "intro_playing"

        if filepath and Path(filepath).is_file():
            fire_id = self._vt_fire_file(filepath, gain_db, "intro")
            with self._vt_lock:
                self._vt["incoming_vt_fire_id"] = fire_id
        else:
            fire_id = None

        # Timing: if the intro-VT is longer than the incoming track's
        # intro window, delay the deck start so intro-VT ends exactly
        # at intro_until. If it's shorter (or absent), start the deck
        # now -- either intro-VT plays over the incoming intro without
        # overshooting, or there's no VT at all.
        if fire_id is not None and vt_duration > intro_until:
            delay_ms = int((vt_duration - intro_until) * 1000)
        else:
            delay_ms = 0
        GLib.timeout_add(max(1, delay_ms), self._vt_start_incoming_now)
        return False   # single-shot timeout

    def _vt_start_incoming_now(self):
        # Reset _next_triggered so _start_next_track can fire naturally.
        self._next_triggered = False
        slot = self._free_slot()
        if slot is not None:
            self._start_next_track(slot=slot)
        with self._vt_lock:
            self._vt["incoming_started"] = True
            no_intro = self._vt.get("incoming_vt_fire_id") is None
        if no_intro:
            # No intro-VT was fired -- the transition is functionally
            # complete once incoming starts. Un-duck now.
            self._vt_complete()
        return False   # single-shot

    def _vt_complete(self):
        cfg = VoiceTrackConfig.load()
        self._vt_ramp_duck_to_db(0.0, cfg.duck_ramp_ms)   # back to unity
        with self._vt_lock:
            self._vt = {"phase": "idle"}
        print("  VT complete (duck released)")

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
                # Deliberately synchronous/blocking -- unlike the
                # recurring auto-build, an operator invoking this
                # expects it to be done by the time it returns.
                now = timezone.localtime()
                next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                log, error = build_and_approve_hour_log_locked(next_hour.date(), next_hour.hour)
                if error and error != LOCK_CONTENDED:
                    print(f"  force_next_hour build failed for {next_hour.date()} {next_hour.hour:02d}:00 -- {error}")
                    emit_event(
                        category="engine", level="warning", title="force_next_hour build failed",
                        detail={"date": str(next_hour.date()), "hour": next_hour.hour, "error": error},
                    )
                elif error == LOCK_CONTENDED:
                    print(f"  force_next_hour deferred for {next_hour.date()} {next_hour.hour:02d}:00 -- already being built elsewhere")
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
            elif cmd == "fx_fire":
                cart_id = data.get("cart_id")
                if cart_id:
                    self._fx_fire(int(cart_id))
            elif cmd == "reload_fx_config":
                # Volume-only reload; polyphony cap changes need a restart
                # because the fx_submix pool sizing is set at build.
                self._fx_apply_volume()
            elif cmd == "reload_voicetrack_config":
                # Duck depth / ramp / gap reads happen at fire time, so
                # a config change lands on the very next VT sequence
                # without a restart. This handler is a no-op today --
                # kept for API symmetry with reload_fx_config and to
                # give the admin form a Save button that isn't
                # misleading. Behavior belongs in _vt_* helpers (Phase
                # 1d-ii).
                pass
        except Exception as exc:
            print(f"  Command error: {exc}")

    def _splice_log_item_db_at(self, insert_at, track, category):
        """DB-only half of a queue splice -- caller must already be
        inside transaction.atomic(). Shifts every not-yet-played item's
        position up by 1 in the DB (two-phase, through a disjoint offset
        range then back down, since LogItem's unique_together=
        ("playlist_log","position") constraint would collide on a
        single ascending "+1" UPDATE), creates the new LogItem, and
        returns it. Caller applies the matching in-memory mutation
        separately via _apply_log_item_insert, ONLY after the
        transaction creating this row has actually committed -- so a
        failure anywhere in that transaction (including something the
        caller does after this call, like a SongRequest claim) rolls
        back cleanly with no in-memory/DB state ever having diverged."""
        if insert_at < len(self.log_items):
            insert_position = self.log_items[insert_at].position
            LogItem.objects.filter(playlist_log=self.current_log, position__gte=insert_position).update(position=F("position") + 100000)
            LogItem.objects.filter(playlist_log=self.current_log, position__gte=insert_position + 100000).update(position=F("position") - 99999)
            new_position = insert_position
        else:
            new_position = (self.log_items[-1].position + 1) if self.log_items else 0
        return LogItem.objects.create(
            playlist_log=self.current_log, position=new_position, scheduled_time=timezone.localtime(),
            track=track, track_title=track.title, track_artist=track.artist.name if track.artist_id else "",
            category=category,
        )

    def _apply_log_item_insert(self, insert_at, new_item):
        """In-memory half -- call ONLY after the transaction that
        created new_item (via _splice_log_item_db_at) has committed."""
        if insert_at < len(self.log_items):
            for item in self.log_items[insert_at:]:
                item.position += 1
        self.log_items.insert(insert_at, new_item)

    @contextmanager
    def _locked_playlist_log(self):
        """Bounded-wait PlaylistLog lock -- same 250ms lock_timeout
        contract as webrequests.services.maybe_schedule_song_request's
        own lock, and for the same reason: this runs on the engine's
        single GLib audio thread, and an unbounded wait here (e.g. if
        refresh_song_request_statuses briefly holds the same row) would
        stall the entire audio engine, not just this one operation.
        Raises OperationalError with SQLSTATE 55P03 on timeout; callers
        decide how to degrade (never propagate unhandled onto the audio
        thread)."""
        with connection.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '250ms'")
        PlaylistLog.objects.select_for_update().get(pk=self.current_log.id)
        yield

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
        ever fired.

        Thin wrapper: the ENTIRE operation, including the Category/Track
        lookups (not just the splice itself), is wrapped so nothing here
        can propagate to _check_commands' outer except-and-print, which
        runs AFTER the command file has already been deleted -- a
        genuine DB error escaping here would mean the alert is silently
        gone for good, exactly the wrong degradation for a weather/AMBER
        feature. Any failure routes to a bounded retry instead."""
        try:
            self._insert_urgent_next_inner(category_code)
        except Exception as exc:
            self._schedule_urgent_retry(category_code, exc)

    def _insert_urgent_next_inner(self, category_code):
        if not self.current_log or not self.log_items:
            raise RuntimeError("No live log loaded")
        close_old_connections()
        category = Category.objects.get(code=category_code)
        track = (
            Track.objects.filter(category=category, ready2air=True)
            .select_related("artist")
            .first()
        )
        if track is None:
            raise RuntimeError(f"No ready2air track in category {category_code!r}")

        with transaction.atomic():
            with self._locked_playlist_log():
                if self._forced_next_items:
                    # A dedication follow-up (or another urgent item) is
                    # already queued ahead of the normal cursor --
                    # _next_queue_item() won't consult self.log_items
                    # again until that list drains, so splicing into it
                    # via the normal insert_at wouldn't actually change
                    # what plays next. Splice at the cursor as usual (so
                    # the LogItem exists and is positioned correctly for
                    # if/when the forced list ever does drain back to
                    # normal cursor logic), but the urgent item's actual
                    # PLAY ORDER is controlled by prepending to the
                    # forced list below, safety-first ahead of whatever
                    # was already there.
                    insert_at = self._queue_cursor
                else:
                    insert_at = self._queue_cursor
                urgent_item = self._splice_log_item_db_at(insert_at, track, category)

        self._apply_log_item_insert(insert_at, urgent_item)
        if self._forced_next_items:
            self._queue_cursor += 1
            self._forced_next_items.insert(0, urgent_item)  # safety-first: ahead of a pending dedication follow-up
        print(f"  Inserted urgent track ({category_code}) at queue position {insert_at}: {track.title}")
        self._urgent_retry_counts[category_code] = 0

    def _schedule_urgent_retry(self, category_code, exc):
        """Per-category retry count/timer -- not one shared counter, so
        two different alert categories contending around the same time
        can't reset or mis-attribute each other's retry/failure
        accounting."""
        pgcode = getattr(getattr(exc, "__cause__", None), "pgcode", None)
        reason = "contended" if pgcode == "55P03" else f"failed ({exc})"
        print(f"  insert_urgent {reason}, retrying shortly ({category_code})")
        count = self._urgent_retry_counts.get(category_code, 0) + 1
        self._urgent_retry_counts[category_code] = count
        if count > 5:
            emit_event(
                category="engine", level="error", title="Urgent insert repeatedly failed",
                detail={"category": category_code, "last_error": str(exc)},
                dedupe_key=f"engine|urgent-contended|{category_code}",
            )
            self._urgent_retry_counts[category_code] = 0
            return
        GLib.timeout_add_seconds(1, lambda: self._insert_urgent_next(category_code) or False)

    def _maybe_insert_dedication_intro(self, log_item):
        """Thin, blanket-fail-open wrapper matching maybe_schedule_song_
        request's own established shape exactly -- a bug in this feature
        must never be able to stop the requested song from starting.
        Every failure, not just lock contention, is caught here: the two
        lookup queries inside the inner function run BEFORE the locked
        block and are just as capable of raising as anything inside it."""
        try:
            return self._maybe_insert_dedication_intro_inner(log_item)
        except OperationalError as exc:
            pgcode = getattr(getattr(exc, "__cause__", None), "pgcode", None)
            message = "Dedication splice lock contended" if pgcode == "55P03" else "Dedication splice database failure"
            print(f"  {message} for log_item={log_item.id}; playing song plainly")
            emit_event(
                category="webrequests", level="warning" if pgcode == "55P03" else "error",
                title=message, detail={"log_item_id": log_item.id, "error": str(exc), "pgcode": pgcode},
                dedupe_key=f"webrequests|dedication-splice|{log_item.id}|{pgcode}",
            )
            return log_item
        except Exception as exc:
            print(f"  Dedication splice failed for log_item={log_item.id}; playing song plainly: {exc}")
            emit_event(
                category="webrequests", level="error", title="Dedication splice failed",
                detail={"log_item_id": log_item.id, "error": repr(exc)},
                dedupe_key=f"webrequests|dedication-splice-error|{log_item.id}",
            )
            return log_item

    def _maybe_insert_dedication_intro_inner(self, log_item):
        if log_item is None or log_item.category_id is None or log_item.category.kind.code != "music":
            return log_item

        close_old_connections()
        already_spliced = SongRequest.objects.filter(
            status="scheduled", log_item_id=log_item.id, intro_log_item__isnull=False,
        ).exists()
        if already_spliced:
            return log_item

        candidate = (
            SongRequest.objects.filter(
                log_item_id=log_item.id, status="scheduled",
                intro_track__isnull=False, intro_track__ready2air=True,
            )
            .order_by("submitted_at").first()
        )
        if candidate is None:
            # Covers "not yet synthesized" and "scheduled by the engine's
            # own last-second safety net, no lead time at all" alike --
            # both correctly fall through to plain playback, per the
            # explicit best-effort delivery policy.
            return log_item

        insert_at = self._queue_cursor - 1
        with transaction.atomic():
            with self._locked_playlist_log():
                locked = (
                    # of=("self",) -- same fix as webrequests.services.
                    # maybe_schedule_song_request's own lock: Postgres
                    # refuses FOR UPDATE across an outer join, and
                    # intro_track (SET_NULL, nullable) makes the
                    # select_related below exactly that once combined
                    # with select_for_update.
                    SongRequest.objects.select_for_update(of=("self",))
                    .filter(id=candidate.id, status="scheduled", log_item_id=log_item.id, track_id=log_item.track_id)
                    .select_related("intro_track", "intro_track__category")
                    .first()
                )
                if locked is None or locked.intro_log_item_id is not None:
                    return log_item
                intro_item = self._splice_log_item_db_at(insert_at, locked.intro_track, locked.intro_track.category)
                # Slot-wide claim: protects EVERY collapsed request sharing
                # this log_item, not just `locked` -- a listener whose
                # request collapsed into someone else's slot is still
                # covered when the song comes back around as a forced
                # follow-up.
                claimed = SongRequest.objects.filter(
                    status="scheduled", log_item_id=log_item.id, track_id=log_item.track_id,
                ).update(intro_log_item=intro_item, status_updated_at=timezone.now())
                if claimed == 0:
                    # Shouldn't happen given the lock above matched `locked`
                    # moments earlier -- but if a stale/manual inconsistency
                    # somehow means nothing takes the claim, raising here
                    # rolls back BOTH the position shift and the just-
                    # created intro LogItem automatically (still inside the
                    # transaction) -- far better than committing an intro
                    # no SongRequest points back to, invisible to restart
                    # recovery.
                    raise RuntimeError(f"Dedication splice created no request claim for log_item={log_item.id}")

        self._apply_log_item_insert(insert_at, intro_item)
        self._queue_cursor = insert_at + 1
        self._forced_next_items.append(log_item)
        return intro_item

    def _restore_followup_for_intro(self, intro_item):
        """Called unconditionally for ANY Dedications item about to
        play, regardless of which path got it there (forced list,
        ordinary cursor walk after a restart, resume-hint recovery).
        Fail-safe and returns a boolean -- an existing Dedications item
        should not air at all unless its paired song can be positively
        identified and protected here. Playing an orphaned announcement
        (no way to tell what it was introducing, or a DB error while
        checking) is worse than skipping straight to whatever's
        genuinely next."""
        try:
            close_old_connections()
            req = (
                SongRequest.objects.filter(
                    intro_log_item_id=intro_item.id, status="scheduled",
                    log_item__isnull=False, log_item__played_at__isnull=True,
                )
                .select_related(
                    "log_item", "log_item__track", "log_item__track__artist",
                    "log_item__track__category", "log_item__track__category__kind",
                    "log_item__category", "log_item__category__kind",
                )
                .first()
            )
        except Exception as exc:
            print(f"  Dedication follow-up lookup failed for intro_item={intro_item.id}: {exc}")
            emit_event(
                category="webrequests", level="error", title="Dedication follow-up lookup failed",
                detail={"intro_log_item_id": intro_item.id, "error": repr(exc)},
                dedupe_key=f"webrequests|dedication-followup-error|{intro_item.id}",
            )
            return False

        if req is None:
            return False

        # Confirming the SongRequest row exists isn't enough -- "protected"
        # means the song can actually be handed to _create_deck. Without
        # this, a file that goes missing between splice and dequeue would
        # still return True here, the intro would air, and only THEN would
        # _next_queue_item's own playability check (reached when the
        # forced song is popped) discover the problem and skip it --
        # exactly the orphaned-intro case this function exists to prevent.
        playable, reason = _log_item_playable(req.log_item)
        if not playable:
            print(f"  Dedication follow-up unplayable for intro_item={intro_item.id}: {reason}")
            emit_event(
                category="webrequests", level="warning", title="Dedication follow-up is unplayable",
                detail={"intro_log_item_id": intro_item.id, "song_log_item_id": req.log_item_id, "reason": reason},
                dedupe_key=f"webrequests|dedication-followup-unplayable|{intro_item.id}",
            )
            return False

        if not any(i.id == req.log_item_id for i in self._forced_next_items):
            self._forced_next_items.append(req.log_item)
        return True

    def _restore_dedication_sequence_from_resume_hint(self):
        """Startup-only -- called from start(), right after
        _apply_resume_hint_queue_rewind(). Blanket try/except: an
        unhandled exception here would prevent the engine from starting
        at all over an optional recovery feature failing, categorically
        worse than just not recovering the pairing this one time.

        Covers the case _restore_followup_for_intro alone can't: a crash
        while the intro was actively on a deck at the moment of the
        crash (identified via the saved engine_state.json resume hint),
        including across an hour boundary -- distinct from, not
        redundant with, _restore_followup_for_intro's own coverage of
        "crash after the splice commits but before the intro's own deck
        is ever created," reached via the ordinary cursor walk."""
        hint = getattr(self, "_resume_hint", None)
        if not hint or not hint.get("log_item_id"):
            return
        try:
            req = (
                SongRequest.objects.filter(
                    intro_log_item_id=hint["log_item_id"], status="scheduled",
                    log_item__isnull=False, log_item__played_at__isnull=True,
                )
                .select_related(
                    "intro_log_item", "intro_log_item__track", "intro_log_item__track__artist",
                    "intro_log_item__track__category", "intro_log_item__track__category__kind",
                    "intro_log_item__category", "intro_log_item__category__kind",
                    "log_item", "log_item__track", "log_item__track__artist",
                    "log_item__track__category", "log_item__track__category__kind",
                    "log_item__category", "log_item__category__kind",
                )
                .first()
            )
            if req is None:
                return
            existing_ids = {i.id for i in self._forced_next_items}
            restored = [i for i in (req.intro_log_item, req.log_item) if i.id not in existing_ids]
            self._forced_next_items = restored + self._forced_next_items
        except Exception as exc:
            print(f"  Dedication sequence restore failed at startup (non-fatal): {exc}")
            emit_event(
                category="webrequests", level="error", title="Dedication sequence restore failed at startup",
                detail={"error": repr(exc)}, dedupe_key="webrequests|dedication-startup-restore-error",
            )

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
            self._remote_dj_gate_open()  # persistent-slot accessor
        )
        any_live = self.mic_live or remote_live
        target = (10 ** (cfg.duck_level_db / 20.0)) if (cfg.enabled and any_live) else 1.0
        self._start_duck_ramp(target)

    def _remote_dj_gate_open(self):
        """True iff a DJ session is currently active AND its slot's
        gate volume is nonzero -- i.e., the remote DJ is broadcasting
        on-air right now. Post-refactor accessor: the gate lives on
        the persistent slot, not on the session (see RemoteDJSlot).
        Callers just want "is a remote mic live?" for ducking and
        mic-mode-hold decisions; this hides the slot lookup and
        None-checks in one place."""
        s = self.remote_dj_session
        if s is None or s.slot_id is None or s.slot_id >= len(self.dj_slots):
            return False
        return self.dj_slots[s.slot_id].remote_gate.get_property("volume") > 0.0

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
            self._remote_dj_gate_open()  # persistent-slot accessor
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
    # Remote DJ over WebRTC
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

        # Claim a persistent DJ slot. With MAX_DJ_SLOTS=1 today this is
        # equivalent to "one at a time" -- the check above already
        # rejects a second attempt, but this belt-and-braces enforces
        # slot-pool semantics so multi-slot (future) won't need this
        # code re-shaped.
        slot = self._dj_slot_available()
        if slot is None:
            print("  Remote DJ session start requested but all DJ slots are occupied — ignoring")
            return False

        print(f"  Remote DJ: session starting (claiming slot {slot.slot_id})")
        session = RemoteDJSession()
        session.slot_id = slot.slot_id
        slot.session = session
        # Update the slot's gain from the current RemoteDJAudioInput
        # config now (fresh read at session start; changes take effect
        # on the next connect, same contract as before). Gate stays at
        # 0 until the DJ explicitly opens it via _remote_dj_set_gate.
        slot.remote_gain.set_property(
            "volume", 10 ** (RemoteDJAudioInput.load().gain_db / 20.0)
        )
        slot.remote_gate.set_property("volume", 0.0)
        self.remote_dj_session = session
        # Open the two diagnostic sinks. Truncate on each session start
        # so a post-mortem sees only THIS session's data (the previous
        # session's log is only interesting up until this one begins).
        # Both opens are best-effort -- if /run/isadoraair isn't
        # writable we skip diag entirely and let the session proceed.
        try:
            DJ_DIAG_LOG.parent.mkdir(parents=True, exist_ok=True)
            session.diag_fh = open(DJ_DIAG_LOG, "w")
            _dj_diag(session, f"session_start")
        except OSError as exc:
            print(f"  Remote DJ: could not open diag log ({exc})")
        try:
            session.dump_fh = open(DJ_DUMP_PCM, "wb")
        except OSError as exc:
            print(f"  Remote DJ: could not open PCM dump file ({exc})")
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
            # Persistent-slot refactor: rollback releases the slot back
            # to the pool but NEVER touches master_mixer (the slot's
            # master_mixer pad is persistent). Reset the slot's gate to
            # 0 defensively in case anything mid-build had already
            # opened it, and switch the selector to silence in case any
            # partial link left it pointing at a WebRTC pad with no
            # upstream.
            if session.slot_id is not None:
                slot_rb = self.dj_slots[session.slot_id]
                slot_rb.session = None
                slot_rb.remote_gate.set_property("volume", 0.0)
                slot_rb.selector.set_property("active-pad", slot_rb.silence_pad)
                peer = slot_rb.webrtc_pad.get_peer()
                if peer is not None:
                    peer.unlink(slot_rb.webrtc_pad)
            for el in session.elements:
                el.set_state(Gst.State.NULL)
                if el.get_parent() is self.main_pipeline:
                    self.main_pipeline.remove(el)
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
        if session.slot_id is None or session.slot_id >= len(self.dj_slots):
            print("  Remote DJ pad-added but no slot claimed on the session — ignoring")
            return
        slot = self.dj_slots[session.slot_id]

        # Persistent-slot refactor: the DJ subchain now ends at the
        # slot's pre-allocated selector.sink_1 -- everything downstream
        # (post_conv, gain, gate, out_conv, master_mixer pad) already
        # exists and has been PLAYING silence since engine startup.
        # This function builds ONLY the ephemeral piece: the webrtcbin
        # -fed opus decode + resample + capsfilter chain that hands
        # PCM into the selector. On disconnect this chain is torn down
        # and the selector reverts to silence.
        depay = Gst.ElementFactory.make("rtpopusdepay", None)
        dec = Gst.ElementFactory.make("opusdec", None)
        # Diagnostic level meter kept in place -- still useful (cheap;
        # confirms what the Opus decoder is actually producing).
        session.dj_level = Gst.ElementFactory.make("level", None)
        session.dj_level.set_property("interval", 100_000_000)  # 100ms
        session.dj_level.set_property("post-messages", True)
        conv = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)
        capsfilter = Gst.ElementFactory.make("capsfilter", None)
        # opusdec's output is 48kHz-native; resample DOWN to the
        # pipeline rate here. Match the selector's other input (silence)
        # exactly on caps so the switch is seamless.
        capsfilter.set_property("caps", self._remote_dj_pipeline_caps())
        queue = Gst.ElementFactory.make("queue", None)
        for el in (depay, dec, session.dj_level, conv, resample, capsfilter, queue):
            self.main_pipeline.add(el)
            session.elements.append(el)
        depay.link(dec)
        dec.link(session.dj_level)
        session.dj_level.link(conv)
        conv.link(resample)
        resample.link(capsfilter)
        capsfilter.link(queue)
        # Link the queue's src to the slot's pre-allocated webrtc_pad on
        # the input-selector. This is a plain pad-to-pad link on an
        # already-existing pad -- no request_pad_simple on the mixer,
        # no aggregator-state mutation, no race.
        queue.get_static_pad("src").link(slot.webrtc_pad)
        pad.link(depay.get_static_pad("sink"))

        for el in (depay, dec, session.dj_level, conv, resample, capsfilter, queue):
            el.sync_state_with_parent()

        _dj_diag(session, "decode_chain_wired (linked to slot.webrtc_pad)")

        # Post-refactor: master_mixer's sink pad is already linked (via
        # the persistent slot chain that has been streaming silence
        # since engine startup), so there is NO "wait for first buffer
        # before linking mixer" gating step here anymore -- that whole
        # class of race is structurally gone. All we need is to flip
        # the selector's active-pad from silence to webrtc-audio the
        # moment the decode chain has real audio to deliver. A BUFFER
        # probe on the queue's src pad detects the first real buffer
        # and does exactly that. If we flipped immediately here instead
        # of waiting, the selector would switch to sink_1 before it has
        # produced any data, and the momentary underrun would surface
        # as ~few-ms of drift while opusdec spins up -- the probe
        # eliminates it.
        queue_src = queue.get_static_pad("src")
        session.dj_mixer_pad_src = queue_src

        # DIAGNOSTIC PROBE 1 -- caps events. Every caps event on this
        # pad gets timestamped and dumped to DJ_DIAG_LOG. If a caps
        # renegotiation fires during the audible-static window, the
        # log will show it with the exact old->new caps -- diagnostic
        # for the "sample-format mismatch caused garbage buffer to be
        # interpreted as PCM" hypothesis.
        #
        # DEFENSIVELY WRAPPED: a pad-probe callback on a hot audio
        # pad that throws will disrupt event propagation and can
        # cascade into a not-negotiated state elsewhere in the
        # pipeline. Learned the hard way 2026-07-20 10:15 when the
        # initial version of this probe used the wrong parse_caps()
        # unpack shape for this PyGObject version and killed on-air
        # audio, requiring an engine restart. NO diagnostic probe is
        # ever worth taking audio off-air; catch everything, log a
        # note, and return OK.
        def _caps_log_probe(probed_pad, info, _u):
            try:
                s = self.remote_dj_session
                if s is not session:
                    return Gst.PadProbeReturn.REMOVE
                evt = info.get_event()
                if evt is not None and evt.type == Gst.EventType.CAPS:
                    caps = evt.parse_caps()  # returns Caps directly, NOT a tuple
                    caps_str = caps.to_string() if caps is not None else "(none)"
                    _dj_diag(s, f"gate_conv_src caps={caps_str}")
            except Exception as exc:
                # Log-once and disable this probe -- broken diagnostic
                # is strictly worse than none.
                try:
                    _dj_diag(session, f"caps_log_probe DISABLED after exception: {exc!r}")
                except Exception:
                    pass
                return Gst.PadProbeReturn.REMOVE
            return Gst.PadProbeReturn.OK
        session.caps_probe_id = queue_src.add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, _caps_log_probe, None,
        )

        # DIAGNOSTIC PROBE 2 -- continuous raw-PCM dump of what the
        # master_mixer actually sees, S16LE 44.1kHz stereo. Installed
        # on the MASTER_MIXER SINK PAD (not on gate_conv src) so we
        # get every buffer that reaches the mixer without competing
        # with the BLOCK_DOWNSTREAM first-buffer probe on the source
        # side -- the previous placement dumped only ~100ms because
        # the block-and-unblock cycle appeared to interfere with the
        # source-pad BUFFER probe firing reliably afterward. Sink-pad
        # observation is cleaner: the mixer's sink pad only exists
        # AFTER _on_first_buffer_ready links it, so we install the
        # probe there in one place, at the right time.
        #
        # Now runs for the WHOLE session, not first 1s -- catches the
        # bug even if it fires mid-session after the DJ opens the
        # gate. A typical session's PCM footprint is ~176 KB/s =
        # ~10 MB/min, fine on tmpfs.
        # Replay via:
        #   ffplay -f s16le -ar 44100 -ac 2 /run/isadoraair/remote_dj_first_1s.pcm
        # or import in Audacity as raw S16LE stereo 44100.

        def _on_first_buffer_ready(probed_pad, info, _u):
            """Fires when opusdec's first decoded buffer reaches the
            end of the ephemeral decode chain. All this does now is
            flip the slot's input-selector active-pad from silence to
            the WebRTC path. No master_mixer touching, no pad request,
            no offset math, no aggregator-state mutation -- the whole
            class of bug that this function used to be structured
            around is gone."""
            try:
                s = self.remote_dj_session
                if s is not session or s.slot_id is None:
                    return Gst.PadProbeReturn.REMOVE
                slot2 = self.dj_slots[s.slot_id]
                slot2.selector.set_property("active-pad", slot2.webrtc_pad)
                print("  Remote DJ: slot selector switched from silence to WebRTC audio")
                _dj_diag(s, f"slot {slot2.slot_id} selector switched to webrtc_pad (silence -> live)")
            except Exception as exc:
                try:
                    _dj_diag(session, f"first_buffer_ready EXCEPTION (removing probe): {exc!r}")
                except Exception:
                    pass
                print(f"  Remote DJ first-buffer probe exception (removing): {exc}")
            return Gst.PadProbeReturn.REMOVE
        queue_src.add_probe(
            Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER,
            _on_first_buffer_ready, None,
        )

        # Continuous PCM dump probe -- unchanged intent (post-mortem of
        # what audio actually flowed during this session) but now
        # installed on queue_src (last element before the selector)
        # rather than on a master_mixer sink pad that doesn't exist
        # in the new design. Data is what the slot's selector sees on
        # its webrtc_pad, which is what actually mixes into the master.
        _DUMP_LOG_EVERY_BYTES = 44100 * 2 * 2  # ~1s of stereo S16
        def _dump_probe(probed_pad2, info2, _u2):
            try:
                s2 = self.remote_dj_session
                if s2 is not session or s2.dump_fh is None:
                    return Gst.PadProbeReturn.REMOVE
                buf = info2.get_buffer()
                if buf is not None:
                    ok, mapinfo = buf.map(Gst.MapFlags.READ)
                    if ok:
                        try:
                            s2.dump_fh.write(bytes(mapinfo.data)[:mapinfo.size])
                            s2.dump_bytes_written = getattr(s2, "dump_bytes_written", 0) + mapinfo.size
                            last_marked = getattr(s2, "dump_last_marked_bytes", 0)
                            if s2.dump_bytes_written - last_marked >= _DUMP_LOG_EVERY_BYTES:
                                _dj_diag(s2, f"dump_progress bytes={s2.dump_bytes_written} (~{s2.dump_bytes_written/176400:.2f}s of audio)")
                                s2.dump_last_marked_bytes = s2.dump_bytes_written
                        except OSError:
                            pass
                        finally:
                            buf.unmap(mapinfo)
            except Exception as exc:
                try:
                    _dj_diag(session, f"dump_probe DISABLED after exception: {exc!r}")
                except Exception:
                    pass
                return Gst.PadProbeReturn.REMOVE
            return Gst.PadProbeReturn.OK
        try:
            session.dump_probe_id = queue_src.add_probe(
                Gst.PadProbeType.BUFFER, _dump_probe, None,
            )
            _dj_diag(session, "dump probe installed on queue src (feeding slot selector)")
        except Exception as exc:
            _dj_diag(session, f"dump probe INSTALL FAILED: {exc!r}")

        print("  Remote DJ: decode chain wired; waiting for first buffer to flip slot selector")
        session.real_buf_seen = True

    def _remote_dj_on_connection_state(self, element, _pspec):
        session = self.remote_dj_session
        if session is None or session.webrtc is not element:
            return
        state = element.props.connection_state
        nick = state.value_nick
        print(f"  Remote DJ connection state: {nick}")
        _dj_diag(session, f"webrtc connection_state -> {nick}")
        # Breadcrumb on every state edge -- rare per-session, cheap to
        # emit, invaluable the next time a WebRTC-adjacent bug turns up
        # in the wild and we need to reconstruct the sequence.
        emit_event(
            category="engine",
            level="info",
            title=f"Remote DJ connection: {nick}",
            detail={"state": nick},
            dedupe_key=f"engine|remote_dj|state={nick}",
        )
        if state == GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED:
            # Transient. The browser may ICE-restart back to CONNECTED
            # (WiFi<->cellular handover is the canonical case). Don't
            # tear down yet, but DO force the gate to 0 so any noise
            # squeezed out of the stalled decode chain -- whether that's
            # uninitialized-buffer memory from downstream pool
            # allocations, clock-slave drift in webrtcbin's internal
            # rtpjitterbuffer bursting timing-wrong frames, or
            # Opus concealment if it ever gets turned on -- gets
            # multiplied by zero at this stage instead of summing into
            # master_mixer and drowning the playing deck in static.
            # This is the "elusive noise on deck" bug the user hit
            # infrequently on network handover mid-session. gate_desired
            # is preserved so _remote_dj_on_connection_state's CONNECTED
            # branch can restore the DJ's intent on recovery.
            slot_dc = self.dj_slots[session.slot_id] if session.slot_id is not None else None
            if slot_dc is not None and slot_dc.remote_gate.get_property("volume") > 0.0:
                slot_dc.remote_gate.set_property("volume", 0.0)
                self._apply_talk_ducking()
                print("  Remote DJ: gate forced to 0 during transient disconnect (protecting playing deck)")
                emit_event(
                    category="engine",
                    level="warning",
                    title="Remote DJ gate forced to 0 during transient disconnect",
                    detail={"reason": "protective mute during WebRTC DISCONNECTED"},
                    dedupe_key="engine|remote_dj|protective_mute",
                )
        elif state == GstWebRTC.WebRTCPeerConnectionState.CONNECTED:
            # Recovery path -- DJ was live and had the gate open before
            # the blip; restore what they wanted instead of leaving them
            # muted after ICE renegotiates. First-time CONNECTED entry
            # doesn't reopen anything either (gate_desired starts False,
            # gate stays at its 0.0 initial value).
            slot_rc = self.dj_slots[session.slot_id] if session.slot_id is not None else None
            if (session.gate_desired and slot_rc is not None
                    and slot_rc.remote_gate.get_property("volume") == 0.0):
                slot_rc.remote_gate.set_property("volume", 1.0)
                self._apply_talk_ducking()
                print("  Remote DJ: gate restored to 1.0 after reconnect")
        elif state in (GstWebRTC.WebRTCPeerConnectionState.FAILED, GstWebRTC.WebRTCPeerConnectionState.CLOSED):
            self._remote_dj_session_stop()

    def _remote_dj_set_gate(self, active):
        session = self.remote_dj_session
        if session is None or session.slot_id is None:
            print("  remote_dj_gate requested but no session is active — ignoring")
            return
        slot = self.dj_slots[session.slot_id]
        # Remember the DJ's intent BEFORE touching the volume element --
        # if the connection is currently DISCONNECTED and we're being
        # asked to go active, we still record that intent so a later
        # reconnect can restore it, even though the physical volume
        # stays at 0 until the reconnect actually completes.
        session.gate_desired = active
        slot.remote_gate.set_property("volume", 1.0 if active else 0.0)
        self._apply_talk_ducking()
        self._apply_mic_mode_hold()
        print(f"  Remote DJ gate: {'ON' if active else 'OFF'}")

    def _remote_dj_session_stop(self):
        session = self.remote_dj_session
        if session is None:
            return False
        print("  Remote DJ: session stopping")
        self.remote_dj_session = None

        slot = self.dj_slots[session.slot_id] if session.slot_id is not None else None

        # Safety first: close the gate before tearing anything down. In
        # the persistent-slot design the gate is a slot property, not a
        # session property, so it survives session teardown -- just set
        # it back to 0 so the next session opens with the DJ muted.
        if slot is not None:
            slot.remote_gate.set_property("volume", 0.0)
            self._apply_talk_ducking()
            # Also fold any mic-held Manual back to Auto now that the
            # remote is definitively off -- session_stop is one of the
            # implicit "gate goes off" paths where _remote_dj_set_gate
            # isn't called.
            self._apply_mic_mode_hold()

        # Persistent-slot teardown -- structurally simpler than the
        # pre-refactor path:
        # (1) Flip the slot's input-selector active-pad back to the
        #     silence path FIRST. From this instant the slot's
        #     downstream chain is fed by the always-running silence
        #     source rather than the WebRTC decode chain we're about
        #     to tear down. No underrun window for master_mixer.
        # (2) Unlink the ephemeral WebRTC decode chain from the slot's
        #     webrtc_pad. The pad itself stays -- pre-allocated,
        #     ready for the next session.
        # (3) THEN set the ephemeral elements to NULL and remove them.
        # (4) Same monitor-tee handling as before.
        # NOTE: no master_mixer touching at any point. The slot's
        # master_mixer pad has never been released and never will be
        # for the pipeline's lifetime.
        if slot is not None:
            slot.selector.set_property("active-pad", slot.silence_pad)
            peer = slot.webrtc_pad.get_peer()
            if peer is not None:
                peer.unlink(slot.webrtc_pad)
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

        # Release the slot back to the pool -- the ONLY per-session
        # bookkeeping we do on the persistent audio path. The slot's
        # elements (silence source, selector, gain, gate, master_mixer
        # pad) stay intact; they'll continue driving silence into the
        # mixer until the next session claims this slot.
        if slot is not None:
            slot.session = None
            _dj_diag(session, f"slot {slot.slot_id} released back to pool")

        if self._remote_dj_server:
            self._remote_dj_server.disconnect_threadsafe()

        # Close diagnostic sinks -- files stay on disk (not truncated
        # here) so a user reporting "static during that last session"
        # has something to inspect. Next session start truncates them.
        _dj_diag(session, "session_stop")
        if session.diag_fh is not None:
            try:
                session.diag_fh.close()
            except OSError:
                pass
            session.diag_fh = None
        if session.dump_fh is not None:
            try:
                session.dump_fh.close()
            except OSError:
                pass
            session.dump_fh = None

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
            .select_related(
                "track", "track__artist", "track__album", "track__category", "track__category__kind",
                "category", "category__kind",  # LogItem's own category, read directly by dedication logic
            )
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
                emit_event(
                    category="engine",
                    level="critical",
                    title=f"Deck {slot} stuck past EOS on {deck.track.title!r}",
                    detail={
                        "slot": slot,
                        "track_id": deck.track.id,
                        "track_title": deck.track.title,
                        "position_seconds": round(pos, 1),
                        "duration_seconds": round(duration, 1),
                        "overrun_seconds": round(pos - duration, 1),
                    },
                    dedupe_key=f"engine|stuck|slot={slot}|track={deck.track.id}",
                )
                deck.finished = True
                self.decks[slot] = None
                self._next_triggered = False

    @_glib_safe(default_return=True)
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
            next_item = self._peek_playable_at_cursor()
            if next_item is None:
                # Current hour's queue is exhausted — peek at the next
                # hour's already-approved log so the last track of the
                # hour can still crossfade into the first track of the
                # next, instead of always hard-cutting at top of hour.
                peek = self._peek_next_hour()
                if peek:
                    _, next_hour_items = peek
                    # First PLAYABLE item -- if next hour's first log
                    # item is a deleted-track ghost, look past it so
                    # the crossfade still triggers into something real.
                    next_item = next(
                        (it for it in next_hour_items if _log_item_playable(it)[0]),
                        None,
                    )
                elif self._try_extend_live_log():
                    # Real next hour isn't built yet, but a Log Fill
                    # Configuration re-pick just landed on the live log —
                    # pick it up now so the crossfade below can trigger
                    # into it normally instead of waiting for EOS.
                    next_item = self._peek_playable_at_cursor()

            if next_item is not None:
                next_cue_in = next_item.track.cue_in_seconds or 0.0
                trigger_point = next_start - next_cue_in

                if trigger_point < 10.0:
                    trigger_point = next_start

                if pos >= trigger_point - CACHE_WARM_LEAD_SECONDS:
                    self._warm_track_cache(next_item)

                # VT check runs BEFORE the normal trigger since
                # outro_starts typically comes earlier than
                # (next_start - next_cue_in). _vt_maybe_enter sets
                # _next_triggered=True on entry, which suppresses the
                # normal trigger below for the remainder of the
                # transition.
                outro_start = leading.track.outro_starts_seconds
                if (outro_start is not None
                        and pos >= outro_start
                        and self._vt.get("phase") == "idle"
                        and not self._next_triggered
                        and not self.manual_mode):
                    self._vt_maybe_enter(leading)

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

    @_glib_safe(default_return=False)
    def _on_deck_eos_probed(self, deck_bin):
        deck = self._deck_bin_map.get(id(deck_bin))
        if not deck or deck.finished:
            return

        # A flushing seek into compressed audio -- the auto-resume seek
        # on an engine restart, or a manual seek from the wave-canvas UI
        # (_seek_deck/_resume_deck use the identical FLUSH|KEY_UNIT
        # pattern) -- can land on a byte offset the parser can't cleanly
        # resync from. Observed live (2026-07-31/08-01): a
        # `gst_base_parse_finish_frame: assertion 'size > 0 ||
        # frame->out_buffer' failed` right after the seek, immediately
        # followed by a spurious EOS through this exact probe. eos_probe
        # already drops the EOS before it reaches the live mixer, so
        # nothing audible happens from GStreamer's side -- but blindly
        # trusting it here tore the deck down and rebuilt it from
        # position 0, discarding the resume/seek position that had just
        # been applied. A genuine EOS always arrives with position close
        # to the track's own duration; one that arrives far short of it
        # is almost certainly this parser artifact, not a real end of
        # stream, and is safe to ignore -- GStreamer's own pipeline
        # keeps decoding past the hiccup on its own, since nothing was
        # torn down to prevent that. Same DECK_STUCK_TIMEOUT_SECONDS
        # margin _check_stuck_decks already uses for the opposite
        # direction (past duration with no EOS), reused here as "how
        # much slop around the expected duration is plausible" either way.
        duration = deck.track.duration_seconds or 0
        if duration:
            pos = self._get_deck_position(deck)
            if pos < duration - DECK_STUCK_TIMEOUT_SECONDS:
                print(f"  [{deck.slot}] Ignoring implausible EOS at {pos:.1f}s "
                      f"(duration {duration:.1f}s) on {deck.track.title!r} -- "
                      f"likely a post-seek parser hiccup, not a real end of stream")
                emit_event(
                    category="engine", level="warning", title="Ignored implausible EOS",
                    detail={
                        "slot": deck.slot, "track_id": deck.track.id, "track_title": deck.track.title,
                        "position_seconds": round(pos, 1), "duration_seconds": round(duration, 1),
                    },
                    dedupe_key=f"engine|implausible-eos|slot={deck.slot}|track={deck.track.id}",
                )
                return

        self._handle_deck_finished(deck)

    @_glib_safe(default_return=True)
    def _on_deck_error(self, bus, message, deck):
        err, debug = message.parse_error()
        print(f"  GStreamer error on {deck.track.title}: {err} ({debug})")
        emit_event(
            category="engine",
            level="error",
            title=f"GStreamer error on {deck.track.title!r}",
            detail={
                "slot": deck.slot,
                "track_id": deck.track.id,
                "track_title": deck.track.title,
                "error": str(err),
                "debug": debug,
            },
            dedupe_key=f"engine|gst_error|track={deck.track.id}",
        )
        GLib.idle_add(self._handle_deck_finished, deck)
        return True

    @_glib_safe(default_return=False)
    def _handle_deck_finished(self, deck):
        slot = deck.slot

        # VT mode: if the deck ending is the outgoing one in a VT
        # sequence, run the VT branch instead of the normal path. VT
        # takes ownership of "what starts next" via the state machine
        # (see _vt_start_incoming_now), so don't call _start_next_track
        # here.
        vt_phase = self._vt.get("phase")
        vt_outgoing_track_id = self._vt.get("outgoing_track_id")
        if (vt_phase in ("outro_playing", "outro_tail")
                and deck.track.id == vt_outgoing_track_id):
            self._remove_deck(deck)
            self._vt_handle_outgoing_ended()
            return

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

    @_glib_safe(default_return=True)
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

    def _write_rbds_category_state(self, track):
        """Resolves the currently-playing track's Category RBDS PTY/PTYN
        override and writes it to RBDS_CATEGORY_STATE_PATH for
        rbds/services/rbds_manager.py to pick up on its next tick.
        Called from _create_deck() alongside _write_now_playing() --
        same "deck creation is the approximation point" reasoning
        applies here (see that method's own docstring).

        Resolving here (rather than having the RBDS process do its own
        Category lookup) mirrors this method's own existing precedent
        of pre-resolving Track.alt_send_enabled/alt_send_text for the
        stream metadata -- one place decides what the "effective"
        broadcast values are, every consumer just reads the answer."""
        category = track.category
        payload = {
            "pty_override": category.rbds_pty_override if category else None,
            "ptyn": (category.rbds_ptyn if category else "") or "",
        }
        try:
            RBDS_CATEGORY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = RBDS_CATEGORY_STATE_PATH.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(RBDS_CATEGORY_STATE_PATH)
        except OSError:
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
                    # log_item_id is the auto-resume key. The queue
                    # cursor advances past a LogItem the moment its
                    # deck is created, so at any mid-crossfade
                    # moment the cursor is already past whatever
                    # deck A is playing. On restart, _read_resume_hint
                    # uses this to back the cursor UP to whichever
                    # LogItem the crossfading track was on -- so the
                    # correct LogItem loads first and its track_id
                    # matches the hint for the seek. Without this,
                    # a mid-crossfade restart correctly captures the
                    # hint but the wrong LogItem loads first, no
                    # match, no seek.
                    "log_item_id": deck.log_item.id if deck.log_item else None,
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
                # "Effective airtime" = the number of seconds a listener
                # will actually hear this track for, from cue_in to
                # next_start (or duration if next_start is unset). Same
                # number the countdown on the Playing deck decays down
                # from when this track goes live, so the preview deck
                # UI can show the identical figure to prime the DJ.
                q_effective_end = qt.next_start_seconds if qt.next_start_seconds is not None else (qt.duration_seconds or 0)
                q_cue_in = qt.cue_in_seconds or 0
                q_airtime = max(0.0, q_effective_end - q_cue_in)
                queue.append({
                    "item_id": qi.id,
                    "track_id": qt.id,
                    "title": qt.title,
                    "artist": qt.artist.name if qt.artist else "",
                    "duration": qt.duration_seconds or 0,
                    "airtime_seconds": round(q_airtime, 1),
                    "category": qt.category.code if qt.category else "",
                    "format": qt.format or "",
                    "fill_color": qt.category.kind.fill_color if qt.category else None,
                    "eta_seconds": round(eta, 1),
                })
                eta += q_effective_end

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
                "remote_dj_live": self._remote_dj_gate_open(),
            }

            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.rename(STATE_PATH)
        except Exception as exc:
            print(f"Failed to write state: {exc}")
