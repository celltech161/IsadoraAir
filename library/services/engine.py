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
gi.require_version("GstBase", "1.0")
from gi.repository import Gst, GLib, GstBase, GstSdp, GstWebRTC

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
    NOMINAL_HOUR_SECONDS,
    append_fill_items,
    build_and_approve_hour_log_locked,
    effective_airtime_seconds,
    fill_remaining_hour,
    resolve_schedule_block,
)
from library.services.remote_dj_signaling import RemoteDJSignalingServer
from library.services import audio_recovery
from webrequests.services import SCHEDULING_CONTENDED, maybe_schedule_song_request, mark_song_requests_aired
from isadoraair.version_info import capture_runtime_commit

STUDIO_MONITOR_NAME = "Studio Monitor"
STUDIO_MONITOR_FALLBACK_DEVICE = "plughw:2,0"

# StereoTool bridge — a raw (pre-AGC) tap off the mixer, fed to an ALSA
# loopback device StereoTool reads from separately. Unlike Studio Monitor,
# there's no fallback device: if this row has nothing configured, the tee
# branch simply isn't built at all (see _build_main_pipeline).
STEREOTOOL_OUTPUT_NAME = "Stereotool Input"

# [P0] 1.3C -- audio OUTPUT device hotplug recovery. Health rule proven
# empirically against real hardware (scratchpad/audio_output_recovery/
# round6_physical_usb_validation/, see ROUND7_DECISION_REPORT.md) --
# PLAYING held continuously for OUTPUT_HEALTH_STABILIZATION_S, THEN
# (valve opened for verification) GstBaseSink stats()["rendered"] must
# actually increase within OUTPUT_RENDER_VERIFY_DEADLINE_S, or the
# attempt is reported as failed and the valve closes again. Mirrors
# [P0] 1.3B2's locked mic-recovery health rule shape (PLAYING >= 500ms
# AND fresh buffer <= 250ms), re-derived independently for output
# because GstBaseSink's own rendered-count stat is a more direct
# render-side signal than the mic path's buffer-probe-timestamp
# approach -- see Round 6's SUMMARY.md for why PLAYING alone was
# proven NOT sufficient (a real UCA222 rebuild reached PLAYING via
# sync_state_with_parent() while genuinely stuck on a preroll wait).
OUTPUT_HEALTH_STABILIZATION_S = 0.5
OUTPUT_HEALTH_CHECK_DEADLINE_S = 5.0  # generous grace for a real device -- Round 6's real rebuilds took well under this
OUTPUT_RENDER_VERIFY_DEADLINE_S = 2.0  # bounded window, valve open, waiting for stats()["rendered"] to actually increase
OUTPUT_RECOVERY_SLOT_TIMEOUT_S = 15.0  # matches mic's own SlotCoordinator timeout_s

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
# memory; it now covers the whole session -- unbounded by session
# length, ~10 MB/min at S16 stereo 44100 (a multi-hour remote DJ show
# can put several GB on /run's tmpfs, shared with unrelated operational
# state like engine_state.json/levels.json).
DJ_DUMP_PCM = Path("/run/isadoraair/remote_dj_first_1s.pcm")
# This instrumentation was built to hunt the static-on-connect bug
# during Remote DJ connection establishment -- that investigation is
# resolved, so the dump is OFF by default (2026-08-20 audit). Left
# fully wired in, not removed: when a similar issue needs live PCM
# evidence again, flip this back to True (no other code changes
# needed) rather than re-deriving the instrumentation from git
# history. While False, neither the file nor the pad probe below is
# ever created -- zero cost, zero growth.
DJ_DUMP_PCM_ENABLED = False


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

# [4.1] Remote Mic PTT VU meter -- how old the most recent captured
# session.dj_level_sample is allowed to be before _remote_dj_level_
# payload() treats it as gone rather than frozen-stale. Matches the
# client's own existing Program Level staleness literal in
# dashboard.html's pollLevels() (0.75s) -- belt-and-braces on both
# ends of the same file, not two independently-chosen thresholds. A
# session disconnecting (self.remote_dj_session -> None, see
# _remote_dj_session_stop) already clears this on the very next
# output_level tick (~LEVEL_INTERVAL_MS later), well under this
# threshold -- this constant is the second-line defense for the rarer
# case where the session object survives but its audio has genuinely
# stopped flowing (e.g. a WebRTC hiccup).
REMOTE_DJ_LEVEL_STALE_S = 0.75
POSITION_POLL_MS = 250
AUTO_BUILD_CHECK_SECONDS = 10
NEXT_HOUR_LOOKAHEAD_SECONDS = 30
# Clock-drift recovery (1.1 spec): one-way cap on how much shorter than a
# nominal hour the upcoming build's target_duration_seconds can be
# shrunk to compensate for a projected-late takeover. Protects against a
# wildly-off projection (e.g. a stuck/paused deck making the leading-
# deck ETA read as much larger than reality) shrinking the built hour
# to nothing -- a default, not a sacred value. See
# _project_upcoming_hour_target_duration.
MAX_CLOCK_RECOVERY_SECONDS = 600
CACHE_WARM_LEAD_SECONDS = 3.0
SILENCE_PRIME_SECONDS = 0.3
DECK_STUCK_TIMEOUT_SECONDS = 30  # generous margin past a track's own duration before assuming its EOS was missed
# How long after an actual seek (auto-resume or manual) an EOS is
# treated with suspicion by _on_deck_eos_probed. Empirically measured
# via an isolated throwaway-pipeline reproduction of the 2026-08-01
# incident: a flushing KEY_UNIT seek into this station's compressed
# library can trigger a transient gst_base_parse_finish_frame parser
# hiccup, but the pipeline consistently finished renegotiating and
# resumed normal real-time decoding within ~2.7s across 5/5 repro
# runs. 5.0s (not just ~2.7s + a hair) deliberately: that reproduction
# was an isolated pipeline with none of a real engine restart's
# concurrent startup load (StereoTool sink, AGC, RemoteDJ slot setup
# all building around the same time), so it likely underestimates the
# real settle time -- the extra margin costs nothing (an unrelated
# genuine EOS inside this window still only gets scrutinized, not
# blocked, if its position is actually near the track's real end).
SEEK_EOS_GUARD_SECONDS = 5.0
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


def _output_sink_rendered_count(sink):
    """[P0] 1.3C -- non-blocking property read, safe to call from any
    thread (GstBaseSink's `stats` GstStructure, confirmed via
    gst-inspect-1.0 alsasink and read back exactly this way in Round 6's
    physical validation -- get_uint64() returns (bool, value), not a
    bare value). None if the sink has no usable stats (shouldn't happen
    for a real alsasink, but this is used inside a background worker
    where a defensive None is much better than an uncaught exception)."""
    try:
        stats = sink.get_property("stats")
        ok, val = stats.get_uint64("rendered")
        return val if ok else None
    except Exception:
        return None


# Number of concurrent remote-DJ slots the pipeline is built to
# support. Hardcoded to 1 today; the audio-path structure (per-slot
# persistent selector + silence source + gate + gain + master_mixer
# pad) is a list so bumping to N is a constant change plus signaling/
# UI/policy work that lives outside engine.py. Do NOT flip this to
# >1 without matching signaling-server work; the engine will happily
# build the slots but only one WebRTC session can be active at a time
# with the current signaling protocol.
MAX_DJ_SLOTS = 1


class OutputRecoverySlot:
    """[P0] 1.3C -- one independent recovery boundary per physical audio
    output (Studio Monitor, Stereotool Input). Mirrors the mic-recovery
    design's split between a PERSISTENT boundary (queue, errorignore,
    valve -- never rebuilt) and a REPLACEABLE branch generation (the complete stateful
    processing tail plus alsasink for Studio Monitor; sink-only for
    StereoTool, rebuilt on every recovery attempt) -- see
    library/services/audio_recovery.py's SlotCoordinator and
    PlaybackEngine's mic-recovery methods (_build_mic_hw_generation /
    _mic_quiesce_current_generation / _mic_dispatch_rebuild) for the
    proven precedent this reuses.

    A flat-attribute style (like PlaybackEngine's self._mic_* fields)
    doesn't fit here because there are TWO independent slots, not one --
    this holder is deliberately thin (no logic of its own beyond
    __init__ and the tiny device_loss_epoch accessor pair below; all
    OTHER behavior lives in PlaybackEngine's _output_* methods), same
    "thin data holder" precedent as RemoteDJSlot/Deck below.

    device_loss_epoch + its own lock (pre-commit review finding, see
    _output_dispatch_rebuild's docstring for the full race this closes):
    a monotonically increasing counter, bumped on EVERY classified
    device_lost bus error for this slot's current-or-pending generation
    -- regardless of whether SlotCoordinator.mark_degraded() itself
    returns True or False, i.e. even a COALESCED repeat still bumps it.
    Read from a rebuild worker's background thread, written from the
    GLib thread's bus-error handler -- given its own small lock rather
    than relying on CPython's GIL to make a bare int increment/compare
    "happen to be" safe, per the review's explicit instruction not to
    add a new race while closing this one."""

    def __init__(self, name, kind, queue, errorignore, valve, build_generation_fn,
                 legacy_device, identity_kind, identity, current_bin, current_sink):
        self.name = name                      # AudioOutput.name, e.g. "Studio Monitor"
        self.kind = kind                      # short slug for logging/dedupe/SlotCoordinator naming, e.g. "studio_monitor"
        self.queue = queue                    # persistent Gst.Queue, never rebuilt
        self.errorignore = errorignore        # persistent errorignore element, never rebuilt
        self.valve = valve                    # persistent valve element, never rebuilt
        self.build_generation_fn = build_generation_fn  # callable(device) -> (Gst.Bin, alsasink)
        self.coordinator = audio_recovery.SlotCoordinator(f"output-{kind}", timeout_s=OUTPUT_RECOVERY_SLOT_TIMEOUT_S)
        self.legacy_device = legacy_device    # raw AudioOutput.device string (with Studio Monitor's fallback already applied)
        self.identity_kind = identity_kind
        self.identity = identity
        self.current_bin = current_bin        # current hardware-generation Gst.Bin (ghost "sink" pad)
        self.current_sink = current_sink      # current alsasink element -- for property reapplication + stats
        self.pending_bin = None               # a rebuild attempt not yet promoted (or None)
        self.pending_sink = None
        self.pending_validation_epoch = None  # device_loss_epoch() at the moment THIS candidate's rebuild was dispatched --
        # Operator retarget state bridging bounded teardown/rebuild operations.
        self.retarget_requested = False
        self.retarget_in_progress = False
        # a SECOND pre-commit review finding: the worker's own final epoch check (see
        # _output_dispatch_rebuild) closes the race for anything that happens WHILE the worker is
        # still running, but there is a further, narrower TOCTOU window between the worker
        # returning True and SlotCoordinator's own runner() actually transitioning state to OK
        # (a real, if small, gap -- both happen on the worker's background thread, but not
        # atomically with each other or with the GLib thread's own locking), plus the further gap
        # until _output_recovery_tick's next ~300ms poll notices that transition at all. This
        # field is what the SEPARATE, later check in _output_handle_slot_transition's OK-branch
        # compares device_loss_epoch() against, immediately before promoting -- see that method's
        # own comment for why re-checking there (not just inside the worker) is what actually
        # closes this narrower window.
        self.last_observed_slot_state = self.coordinator.state.value
        self.recovery_attempt = 0
        self.next_retry_at = None             # monotonic seconds
        self.device_present = None            # observability only; None = unknown/not probed yet
        self.last_error = None
        self.last_state_change_at = None
        self._device_loss_epoch = 0
        self._device_loss_epoch_lock = threading.Lock()
        # [P0] 1.3C physical-acceptance-failure fix -- a UI-facing loss-
        # episode counter, DELIBERATELY separate from
        # SlotCoordinator.generation (that counter is an internal
        # operation-tagging mechanism, bumped once per dispatched
        # recovery attempt -- NOT a "how many times has this genuinely
        # gone from healthy to lost" count, and must never be used as a
        # SystemEvent dedupe-key spam boundary; see _on_output_error and
        # monitoring/models.py's emit_event). Bumped exactly once per
        # OK->DEGRADED transition (a genuine first-failure notification),
        # included in the "output lost" event's detail for operator
        # traceability even when repeats coalesce into one dashboard row.
        self.loss_episode = 0

        # [P0] 1.3C second physical-acceptance-failure fix -- serial
        # bumped synchronously, on the calling thread, every time
        # _output_request_recovery() actually returns "dispatched" (a
        # NEW operation genuinely started). Lets _output_recovery_tick
        # distinguish "the transition handler I just called dispatched
        # a follow-up operation" from "it didn't", WITHOUT relying on
        # comparing coordinator.state values (which the nested
        # operation's own worker can race past before the handler even
        # returns, on real hardware-teardown timescales -- confirmed by
        # physical UCA222 testing). See _output_request_recovery and
        # _output_recovery_tick's own docstrings for the full mechanism.
        self.recovery_dispatch_serial = 0

    def invalidate_generation(self):
        """Advance the epoch guarding current/pending generation ownership."""
        with self._device_loss_epoch_lock:
            self._device_loss_epoch += 1
            return self._device_loss_epoch

    def record_device_loss(self):
        """Invalidate generations for every classified device-loss message."""
        return self.invalidate_generation()

    def device_loss_epoch(self):
        """Thread-safe read -- called both from the GLib thread (to
        capture the starting epoch at dispatch time) and from a rebuild
        worker's background thread (to check whether a NEW device-loss
        arrived since)."""
        with self._device_loss_epoch_lock:
            return self._device_loss_epoch


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
        # [4.1] Remote Mic PTT VU meter. dj_gain_db is captured ONCE at
        # session start (see _remote_dj_session_start) from the same
        # RemoteDJAudioInput.load().gain_db read that already sets
        # slot.remote_gain's volume -- never re-queried per meter
        # message. dj_level_sample is the most recent gain-adjusted
        # reading from session.dj_level (see _on_element_message),
        # None until the first message arrives. Both are read (never
        # written) by _remote_dj_level_payload().
        self.dj_gain_db = 0.0
        self.dj_level_sample = None
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
        # time.time() of the most recently applied seek (auto-resume in
        # _create_deck, or a manual _seek_deck/_resume_deck), or None if
        # this deck has never been seeked. Read by _on_deck_eos_probed
        # to scope its post-seek EOS-plausibility check to a short
        # window right after an actual seek, rather than distrusting
        # EOS for the deck's whole lifetime.
        self.seeked_at = None
        # PlayEvent row id written at _create_deck; closed out (ended_at
        # + duration_played_seconds) at _remove_deck. None if the write
        # failed at deck creation -- in which case no close-out attempt
        # is made either, avoiding a spurious update against a row that
        # doesn't exist.
        self.play_event_id = None


class PlaybackEngine:
    def __init__(self):
        # Release/version-skew visibility (1.7 roadmap item): captured
        # exactly ONCE, here, at process construction -- fixed for this
        # engine process's entire lifetime regardless of later checkout
        # changes. Written into every _write_state() tick so
        # /monitoring/'s Playback Engine card can compare it against
        # the CURRENT checkout and show a compact mismatch indicator
        # without ever re-deriving it live. See isadoraair/version_info.py.
        self._runtime_commit = capture_runtime_commit()
        Gst.init(None)
        self.loop = GLib.MainLoop()
        self.mixer = None
        # Cached settings applied to every disposable Studio Monitor
        # generation. The processing elements themselves live inside the
        # generation so a branch retarget resets their streaming state too.
        self._studio_monitor_agc_settings = None
        # [P0] 1.3C -- output hotplug recovery. Studio Monitor's and
        # Stereotool Input's alsasinks are no longer static self.alsasink/
        # self.stereotool_sink attributes -- each now lives inside its own
        # OutputRecoverySlot (built in _build_main_pipeline), reachable at
        # self._studio_monitor_slot.current_sink /
        # self._stereotool_slot.current_sink (the latter None if
        # StereoTool isn't configured). self._output_slots is the same
        # two slots keyed by `kind`, for the tick methods to iterate.
        self._studio_monitor_slot = None
        self._stereotool_slot = None
        self._output_slots = {}
        self.duck_gain = None
        self.master_mixer = None
        self.mic_ptt_valve = None
        self.mic_ptt_volume = None
        self.mic_gain = None
        self.mic_ok = True
        self.mic_live = False
        self.local_mic_tee = None  # only built if remote_dj + local mic both enabled
        self._mic_bin = None
        # [P0] 1.3B2 -- local input hotplug recovery. self._mic_bin above
        # is now the PERSISTENT outer bin (silence source, quarantine
        # queue, input-selector, mic_gain, mic_ptt_volume, ghost pad --
        # never rebuilt); self._mic_hw_bin is the REPLACEABLE inner bin
        # (alsasrc + convert/resample/caps) nested inside it, rebuilt on
        # every recovery attempt. See _build_mic_chain/_build_mic_hw_
        # generation and library/services/audio_recovery.py.
        self._mic_hw_bin = None
        self._mic_hw_generation = 0
        self._mic_pending_hw_bin = None  # a rebuild attempt not yet health-verified
        self._mic_selector = None
        self._mic_silence_pad = None
        self._mic_device_pad = None
        self._mic_quarantine_q = None
        self._mic_slot = None  # audio_recovery.SlotCoordinator, built in _build_mic_chain
        self._mic_last_observed_slot_state = None
        self._mic_recovery_dispatch_serial = 0
        self._mic_recovery_attempt = 0
        self._mic_next_retry_at = None  # monotonic seconds
        self._mic_device_present = None  # observability only; None = unknown/not probed yet
        self._mic_last_error = None
        self._mic_last_state_change_at = None
        self._mic_identity_kind = ""
        self._mic_identity = ""
        self._mic_legacy_device = ""
        self._mic_buf_last_ns = None
        self._mic_buf_count = 0
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
        self._live_fill_in_progress = False  # guarded by self._lock -- see _try_extend_live_log_async
        self._live_fill_generation = 0  # guarded by self._lock -- bumped on every dispatch, see _try_extend_live_log_async
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

        # [P0] 1.3B2 -- local input hotplug recovery. Two independent
        # timers, both no-ops (return True immediately) if no mic is
        # configured (self._mic_slot stays None). Fast tick drives
        # SlotCoordinator's own non-blocking abandonment detection
        # (~200-500ms cadence per the locked design); slow tick is the
        # device-presence fallback probe (~2s, per the locked design --
        # pyudev event-driven detection is deliberately NOT wired in this
        # phase, see the 1.3B2 report).
        GLib.timeout_add(300, self._mic_recovery_tick)
        GLib.timeout_add_seconds(2, self._mic_presence_probe_tick)

        # [P0] 1.3C -- output hotplug recovery. Same two-timer shape as
        # mic above, but ONE fast/slow tick pair services BOTH output
        # slots (Studio Monitor always exists; Stereotool Input only if
        # configured) -- each slot keeps its own independent
        # SlotCoordinator/backoff state, only the GLib timer itself is
        # shared. No-ops (return True immediately) if self._output_slots
        # is empty, which shouldn't happen in practice since Studio
        # Monitor's slot is always built.
        GLib.timeout_add(300, self._output_recovery_tick)
        GLib.timeout_add_seconds(2, self._output_presence_probe_tick)

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

    def _resolve_mic_identity(self):
        """[P0] 1.3B2 -- separate from _resolve_mic_device() above (left
        completely untouched) so existing callers/tests of that method
        are unaffected. Populates self._mic_identity_kind/_mic_identity/
        _mic_legacy_device, used only by the recovery machinery (presence
        probing + resolve_runtime_device on rebuild). Failure here
        degrades to "legacy only, no automatic recovery" -- never blocks
        mic construction, matching every other config-read in this file."""
        try:
            row = (
                AudioInput.objects
                .filter(name=MIC_INPUT_NAME)
                .values("device", "device_identity_kind", "device_identity")
                .first()
            )
        except Exception as exc:
            print(f"  Failed to read AudioInput identity config ({exc}); automatic recovery disabled")
            row = None
        if not row:
            self._mic_identity_kind = ""
            self._mic_identity = ""
            self._mic_legacy_device = ""
            return
        self._mic_legacy_device = row.get("device") or ""
        self._mic_identity_kind = row.get("device_identity_kind") or ""
        self._mic_identity = row.get("device_identity") or ""
        if self._mic_identity_kind == "alsa_card_id" and self._mic_identity:
            print(f"  AudioInput '{MIC_INPUT_NAME}' automatic recovery enabled "
                  f"(alsa_card_id={self._mic_identity!r})")
        else:
            print(f"  AudioInput '{MIC_INPUT_NAME}' has no stable device identity configured; "
                  f"automatic recovery disabled (device loss still degrades to silence)")

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

    def _build_mic_hw_generation(self, device):
        """[P0] 1.3B2 -- the REPLACEABLE hardware-facing portion of the
        mic chain: alsasrc + convert/resample/caps, wrapped in its own
        nested Gst.Bin with a ghost "src" pad. This is rebuilt fresh on
        every recovery attempt (see _mic_quiesce_current_generation /
        _mic_dispatch_rebuild) -- everything downstream of it (quarantine
        queue, input-selector, mic_gain, mic_ptt_volume, the ghost pad
        that feeds local_mic_tee/master_mixer) is PERSISTENT and never
        touched by recovery. This is the ONLY place mic's alsasrc is ever
        constructed, so provide-clock=False (locked, [P0] 1.3B1) and this
        method's construction shape apply identically to every
        generation, including rebuilds."""
        self._mic_hw_generation += 1
        gen = self._mic_hw_generation
        hw_bin = Gst.Bin.new(f"mic_hw_gen{gen}")
        src = Gst.ElementFactory.make("alsasrc", f"mic_src_gen{gen}")
        src.set_property("device", device)
        # Do not allow removable capture hardware to become the shared main
        # pipeline clock. A vanished ALSA capture device can leave its
        # GstAudioSrcClock selected but frozen, stalling unrelated live
        # branches in this same pipeline (Studio Monitor, StereoTool) even
        # while PLAYING. Empirically validated in scratchpad/audio_recovery/
        # (see PHASE_P0_1.3_DISCOVERY_AND_DESIGN.md) -- this alone is
        # sufficient; GStreamer's normal automatic clock selection then
        # falls through to GstSystemClock on its own. Do not pair this with
        # a pipeline.use_clock() call (that path was tested and retired).
        # LOCKED -- [P0] 1.3B1 -- must apply to every generation, no exceptions.
        src.set_property("provide-clock", False)
        convert = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)
        capsfilter = Gst.ElementFactory.make("capsfilter", None)
        caps = Gst.Caps.from_string(f"audio/x-raw,rate={self.pipeline_sample_rate},channels=2")
        capsfilter.set_property("caps", caps)
        for el in (src, convert, resample, capsfilter):
            hw_bin.add(el)
        src.link(convert)
        convert.link(resample)
        resample.link(capsfilter)
        ghost = Gst.GhostPad.new("src", capsfilter.get_static_pad("src"))
        ghost.set_active(True)
        hw_bin.add_pad(ghost)
        return hw_bin, src

    def _install_mic_eos_quarantine(self, sink_pad):
        """[P0] 1.3B2 -- unconditionally drops EOS arriving from the
        hardware-generation side before it can reach input-selector and
        poison the shared main_pipeline. Armed HERE, at construction of
        the persistent quarantine queue -- never reactively after a
        failure is observed. scratchpad/audio_recovery/ Round 4 proved
        reactive arming is always too late (the EOS is already in flight
        by the time a bus-error handler could react).

        EOS ONLY -- narrowed after pre-commit review. The eight-round
        physical investigation's own suppression counters
        (scratchpad/audio_recovery/hotplug_harness/logs/*.jsonl,
        `eos_suppressed` events) show EXACTLY 6 suppressions across every
        physical round, and every single one was type EOS -- zero
        FLUSH_START, zero FLUSH_STOP, ever. No round or design-doc note
        argued for suppressing flush events either; an earlier draft of
        this method copied the harness helper's broader DROP set (EOS +
        FLUSH_START + FLUSH_STOP) without re-checking that the flush half
        was ever actually evidenced, which it wasn't -- fixed here to
        match only what was observed. FLUSH_START/FLUSH_STOP are real,
        materially different GStreamer events (clearing pending data and
        participating in running-time reset semantics) that this probe
        must not interfere with. Registering only EVENT_DOWNSTREAM (not
        EVENT_FLUSH) means FLUSH_START never even reaches this callback
        at all -- confirmed directly: GStreamer does not deliver
        FLUSH_START to an EVENT_DOWNSTREAM-only probe. FLUSH_STOP DOES
        still reach it (confirmed the same way), so it's explicitly
        passed through unconditionally below, same as every other
        non-EOS event -- never dropped.

        Only affects this one pad (the hardware-facing boundary, upstream
        of quarantine_q) -- the silence branch's own events, on a
        completely different pad, are never touched."""
        state = {"suppressed_count": 0}

        def _probe(pad, info):
            event = info.get_event()
            if event is None:
                return Gst.PadProbeReturn.OK
            if event.type == Gst.EventType.EOS:
                state["suppressed_count"] += 1
                # Rate-limited: log the first one plainly (the interesting
                # case -- "did suppression actually engage") and then only
                # every 50th, so a pathological repeat can't flood stdout
                # the way an output-side error storm can (see [P0] 1.3
                # Round 3/4/8's documented output error-flood finding).
                if state["suppressed_count"] == 1 or state["suppressed_count"] % 50 == 0:
                    print(f"  Mic EOS quarantine: suppressed EOS "
                          f"(count={state['suppressed_count']})")
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        sink_pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, _probe)

    def _mic_buffer_age_ms(self):
        """Non-blocking; reads plain attributes updated by
        _mic_buffer_probe below. None if no buffer has ever been seen
        (e.g. no generation has ever linked in yet)."""
        if self._mic_buf_last_ns is None:
            return None
        return (time.monotonic_ns() - self._mic_buf_last_ns) // 1_000_000

    def _mic_buffer_probe(self, pad, info):
        """Attached ONCE, on quarantine_q's src pad -- downstream of both
        the silence and hardware branches' merge point via input-selector
        is NOT where this lives; it's attached upstream of the selector,
        on quarantine_q's src pad, so it measures ONLY the hardware
        generation's real buffer flow (see _build_mic_chain), independent
        of whichever generation is currently linked -- the old generation
        is always fully unlinked+removed before a new one is linked in
        (see _mic_quiesce_current_generation), so any buffer observed
        here can only be from the CURRENT generation. This is what
        distinguishes 'PLAYING state' from 'buffers actually flowing' for
        the locked health rule (PLAYING >= 500ms AND fresh buffer <=
        250ms) -- see _mic_dispatch_rebuild."""
        self._mic_buf_count += 1
        self._mic_buf_last_ns = time.monotonic_ns()
        return Gst.PadProbeReturn.OK

    def _build_mic_chain(self):
        """Builds the mic capture chain as its own Gst.Bin (not loose
        elements directly in main_pipeline) so a bus watch can be
        scoped to just this bin -- mirrors the deck bins' own
        get_bus()/add_signal_watch()/message::error pattern (see
        _start_next_track/_on_deck_error). Returns [] if no mic device
        is configured; the caller treats an empty list exactly like
        "mic not configured" (self.mic_ptt_valve/self.mic_gain stay
        None).

        [P0] 1.3B2: this bin is now PERSISTENT -- silence source,
        quarantine queue, input-selector, mic_gain, mic_ptt_volume, and
        this bin's own ghost pad (still the exact same thing that feeds
        local_mic_tee/master_mixer -- see _build_main_pipeline, UNCHANGED
        by this phase) are built ONCE here and never rebuilt. Only the
        nested hardware-facing bin (_build_mic_hw_generation) is
        replaceable; recovery swaps that generation in and out without
        ever touching this persistent boundary or the master_mixer-facing
        pad, per the locked design in
        scratchpad/audio_recovery/PHASE_P0_1.3_DISCOVERY_AND_DESIGN.md."""
        mic_device = self._resolve_mic_device()
        if not mic_device:
            return []
        self._resolve_mic_identity()

        # Persistent silence fallback -- must exist before any hardware
        # failure can occur, per the locked design ("the silence fallback
        # must already be present before failure").
        silence_src = Gst.ElementFactory.make("audiotestsrc", "mic_silence_src")
        silence_src.set_property("wave", 4)  # silence
        silence_src.set_property("is-live", True)
        silence_caps = Gst.ElementFactory.make("capsfilter", "mic_silence_caps")
        silence_caps.set_property(
            "caps", Gst.Caps.from_string(f"audio/x-raw,rate={self.pipeline_sample_rate},channels=2"))

        # alsasrc is a genuinely live, hardware-clocked source feeding
        # directly into audiomixer, unlike filesrc/decodebin's buffered
        # pull-push hybrid -- this queue decouples ALSA capture cadence
        # from the mixer's aggregation timing so a scheduling hiccup on
        # one side doesn't glitch the whole mix. NOT leaky (unlike the
        # StereoTool queue above): the mic feeds the *shared* master
        # mixer, so silently dropping mic audio would be an audible
        # glitch in the primary output, unlike the StereoTool branch
        # which is only ever allowed to drop its own copy.
        #
        # [P0] 1.3B2: this queue is now ALSO the EOS-quarantine boundary
        # -- its sink pad is where the hardware generation links in, and
        # is where the unconditional EOS/FLUSH suppression probe is
        # installed (see _install_mic_eos_quarantine). Its config
        # (200ms/unbounded) is unchanged from before this phase.
        queue = Gst.ElementFactory.make("queue", "mic_quarantine_q")
        queue.set_property("max-size-time", 200_000_000)  # 200ms
        queue.set_property("max-size-buffers", 0)
        queue.set_property("max-size-bytes", 0)
        self._mic_quarantine_q = queue

        selector = Gst.ElementFactory.make("input-selector", "mic_selector")
        self._mic_selector = selector

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
        for el in (silence_src, silence_caps, queue, selector, self.mic_gain, self.mic_ptt_volume):
            mic_bin.add(el)

        # Silence branch -- persistent, requested ONCE, held forever.
        silence_src.link(silence_caps)
        self._mic_silence_pad = selector.get_request_pad("sink_%u")
        silence_caps.get_static_pad("src").link(self._mic_silence_pad)
        # Hardware branch's fixed sink pad on the selector -- requested
        # ONCE here too; only the peer bin upstream of quarantine_q ever
        # changes (see _mic_quiesce_current_generation /
        # _mic_dispatch_rebuild). Never released/re-requested by recovery.
        self._mic_device_pad = selector.get_request_pad("sink_%u")
        queue.get_static_pad("src").link(self._mic_device_pad)

        selector.link(self.mic_gain)
        self.mic_gain.link(self.mic_ptt_volume)

        self._install_mic_eos_quarantine(queue.get_static_pad("sink"))
        queue.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._mic_buffer_probe)

        # First hardware generation -- built and linked synchronously here
        # at cold start, same risk/timing profile as before this phase
        # (a bad/absent device still surfaces via the normal async bus-
        # error path a moment after PLAYING, exactly as it always has --
        # see _on_mic_error, which now additionally drives recovery).
        # Only RECOVERY attempts (after an observed failure) go through
        # the guarded background-worker path in _mic_dispatch_rebuild.
        hw_bin, hw_src = self._build_mic_hw_generation(mic_device)
        mic_bin.add(hw_bin)
        hw_bin.get_static_pad("src").link(queue.get_static_pad("sink"))
        self._mic_hw_bin = hw_bin
        selector.set_property("active-pad", self._mic_device_pad)

        ghost_pad = Gst.GhostPad.new("src", self.mic_ptt_volume.get_static_pad("src"))
        mic_bin.add_pad(ghost_pad)

        # mic_bin is a plain Gst.Bin added into self.main_pipeline, not its
        # own Gst.Pipeline (unlike deck.pipeline) -- a Bin has no bus of its
        # own. Error messages from its children are forwarded up to the
        # containing Pipeline's bus instead, so the watch is set up there
        # (see _build_main_pipeline) and filtered to this bin.
        self._mic_bin = mic_bin

        self._mic_slot = audio_recovery.SlotCoordinator("mic", timeout_s=15.0)
        self._mic_last_observed_slot_state = self._mic_slot.state.value
        self._mic_recovery_dispatch_serial = 0

        self.mic_ok = True
        return [mic_bin]

    def _on_element_message(self, bus, message):
        """Element messages from the shared main-pipeline bus. Handles
        BOTH the master output_level (dashboard VU meter) AND the
        remote-DJ session's dj_level (post-opusdec level meter, whose
        readings are appended to DJ_DIAG_LOG for the remote-DJ static
        post-mortem, AND -- [4.1] -- captured as a gain-adjusted sample
        for the Remote Mic PTT button's own VU meter). All other
        element messages are ignored so this stays cheap."""
        structure = message.get_structure()
        if structure is None or structure.get_name() != "level":
            return True

        # Remote-DJ level meter -- log to diag file. Cheap, ~10Hz.
        # Unchanged from before [4.1] -- same fields, same log line.
        session = self.remote_dj_session
        if session is not None and message.src is session.dj_level:
            try:
                peak = list(structure.get_value("peak")) or []
                rms = list(structure.get_value("rms")) or []
                _dj_diag(session,
                          f"dj_level peak={['%.1f' % p for p in peak]} rms={['%.1f' % r for r in rms]}")
            except Exception:
                pass
            # [4.1] Remote Mic PTT VU meter -- capture the most recent
            # sample for the button-fill meter, gain-adjusted by the
            # engine-side Remote DJ gain actually applied to THIS
            # session (session.dj_gain_db, cached once at session
            # start -- see _remote_dj_session_start; never re-queried
            # here). Stored on the session only -- NOT written to
            # LEVELS_PATH from here. output_level's own handler below
            # (already the sole writer of LEVELS_PATH, ~50ms cadence)
            # embeds this in its own next write via
            # _remote_dj_level_payload(), so LEVELS_PATH still has
            # exactly one writer. A malformed/empty level structure
            # fails safe -- session.dj_level_sample is simply left
            # unchanged (or None), never raises out of this handler,
            # independent of the diagnostic block above.
            try:
                sample_rms = list(structure.get_value("rms")) or []
                sample_peak = list(structure.get_value("peak")) or []
                sample_decay = list(structure.get_value("decay")) or []
                gain_db = session.dj_gain_db or 0.0
                session.dj_level_sample = {
                    "ts": time.time(),
                    "rms": [v + gain_db for v in sample_rms],
                    "peak": [v + gain_db for v in sample_peak],
                    "decay": [v + gain_db for v in sample_decay],
                }
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
            # [4.1] Remote Mic PTT VU meter -- existing top-level keys
            # (ts/rms/peak/decay) are UNCHANGED, preserving the contract
            # every current Program Level consumer already relies on.
            # This is the only new key. None (JSON null) whenever there's
            # no active Remote DJ session, no sample has arrived yet, or
            # the sample has gone stale -- see _remote_dj_level_payload's
            # own docstring for exactly which of those applies and why.
            "remote_dj": self._remote_dj_level_payload(),
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

    def _remote_dj_level_payload(self):
        """[4.1] Returns the most recent gain-adjusted Remote DJ level
        sample (see _on_element_message's dj_level capture above) for
        embedding in the shared LEVELS_PATH payload, or None when:
          - there is no active Remote DJ session (self.remote_dj_session
            is None -- set immediately at the TOP of
            _remote_dj_session_stop, before any other teardown work, so
            a disconnect already makes this return None on the very
            next output_level tick, ~LEVEL_INTERVAL_MS later -- never
            "indefinitely frozen");
          - a session exists but no dj_level message has arrived yet
            (session.dj_level_sample is still None); or
          - the most recent sample is older than REMOTE_DJ_LEVEL_STALE_S
            -- a second, independent safety net for the rarer case
            where the session object survives but its audio has
            genuinely stopped flowing (e.g. a WebRTC hiccup), distinct
            from the ordinary disconnect path above.
        Never raises -- called on every output_level tick (~20Hz)."""
        session = self.remote_dj_session
        if session is None:
            return None
        sample = session.dj_level_sample
        if sample is None:
            return None
        if time.time() - sample.get("ts", 0.0) > REMOTE_DJ_LEVEL_STALE_S:
            return None
        return sample

    def _on_main_bus_error(self, bus, message):
        # self.main_pipeline's bus isn't watched for anything else today
        # (deck errors go through each deck's own dedicated Gst.Pipeline
        # bus instead, Remote DJ/FX/VT errors have their own dedicated
        # paths -- see _on_dj_bus_msg/_fx_*/_vt_*, none of which route
        # through here) -- filter to only react to errors that originate
        # inside the mic bin or an output recovery slot, walking up the
        # parent chain for an EXACT match against a known element, never
        # a heuristic/substring match, so this can't misclassify an
        # unrelated pipeline error.
        #
        # [P0] 1.3C: previously this whole handler was only CONNECTED at
        # all when a mic was configured (see _build_main_pipeline) -- a
        # real limitation: output recovery needs this router regardless,
        # and even mic-only setups now go through the same unconditional
        # connect. Checked mic first (existing precedent, unchanged
        # logic), output slots second.
        if self._mic_bin is not None:
            obj = message.src
            while obj is not None:
                if obj == self._mic_bin:
                    return self._on_mic_error(bus, message)
                obj = obj.get_parent()
        for slot in self._output_slots.values():
            if self._output_slot_owns_message_src(slot, message):
                return self._on_output_error(slot, bus, message)
        return True

    def _output_slot_owns_message_src(self, slot, message):
        """[P0] 1.3C -- true iff message.src is (or descends from) one of
        THIS slot's own elements: the persistent queue/errorignore/valve,
        the CURRENT hardware generation, or a PENDING (being-verified)
        generation. Reads slot.current_bin/slot.pending_bin fresh on
        every call (not a captured snapshot) so a stale/late message from
        an ALREADY-detached, already-replaced old generation never
        matches -- once a generation is unlinked+removed it's no longer
        reachable via slot.current_bin either, so this can't accidentally
        act on a late result from an abandoned generation (the bus-
        routing side of the same invariant SlotCoordinator's own
        generation-tagging already enforces on the coordinator side).

        [P0] 1.3C physical-acceptance-failure fix -- that
        "no longer reachable via slot.current_bin" claim was aspirational
        until this fix: _output_quiesce_current_generation used to unlink
        +remove the old bin WITHOUT ever clearing slot.current_bin itself,
        so this method kept matching the remaining ALSA error burst from
        an already-detached generation against the slot, long after it
        stopped being current -- confirmed as the root of a real
        production event-storm (gen0->gen563 in ~2.5s off one physical
        unplug). slot.current_bin/current_sink are now retired (set to
        None) at detach time specifically so THIS lookup's own fresh-read
        guarantee actually holds -- see that method's docstring."""
        known = [slot.queue, slot.errorignore, slot.valve, slot.current_bin]
        if slot.pending_bin is not None:
            known.append(slot.pending_bin)
        obj = message.src
        while obj is not None:
            for k in known:
                if k is not None and obj == k:
                    return True
            obj = obj.get_parent()
        return False

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
        self._mic_last_error = str(err)[:300]
        self._mic_last_state_change_at = time.time()

        classification = audio_recovery.classify_audio_device_error(str(err), str(debug))
        if classification != "device_lost":
            # "transient" (e.g. a busy/EBUSY-style resource contention --
            # see audio_recovery.py's own docstring on why this category
            # exists) and "unknown" are logged/observable only, never
            # destructive by default -- [P0] 1.3B2's locked policy.
            print(f"  Mic error classified '{classification}' -- logged only, no recovery action")
            return True

        # Silence is the safe reaction to a runtime mic fault -- not
        # mixer-pad surgery. This codebase's own seek/resume history
        # already shows unlinking a live-mixer-linked bin mid-flight is
        # the riskier operation, reserved for planned transitions, not
        # error recovery. Done unconditionally, every time this fires
        # (even a coalesced repeat for the same already-degraded
        # generation) -- cheap, idempotent, and must never wait on
        # anything below.
        self.mic_ok = False
        self._set_mic_ptt(False)  # mic_live=False, PTT volume=0, ducking + manual-mode-hold released exactly as a normal mic-off would
        if self._mic_selector is not None and self._mic_silence_pad is not None:
            self._mic_selector.set_property("active-pad", self._mic_silence_pad)

        if self._mic_slot is None:
            return True
        first_failure = self._mic_slot.mark_degraded()
        if not first_failure:
            # Coalesced repeat (e.g. the well-known immediate follow-up
            # "Internal data stream error" that always accompanies a real
            # disconnect -- see audio_recovery.py's classifier docstring)
            # -- the mic-off/silence steps above already ran again above
            # (harmless, idempotent); no new teardown is dispatched.
            return True

        print(f"  Mic slot DEGRADED (generation {self._mic_slot.generation})")
        self._mic_recovery_attempt = 0
        self._mic_next_retry_at = None
        self._mic_device_present = None
        emit_event(
            category="hardware", level="warning", title="Studio microphone lost",
            detail={"error": self._mic_last_error},
            dedupe_key=f"hardware|mic-lost|gen{self._mic_slot.generation}",
        )
        self._mic_quiesce_current_generation()
        return True

    def _mic_request_recovery(self, worker):
        """Dispatch one guarded mic operation and preserve its observer edge.

        This is the microphone equivalent of _output_request_recovery.
        SlotCoordinator can finish a fast operation entirely between two
        _mic_recovery_tick polls. Recording RECOVERING synchronously for
        every newly dispatched operation guarantees the next tick observes
        that operation's eventual completion instead of comparing a stale
        OK baseline with a new OK result and skipping the transition.

        The serial also protects a dispatch made inside
        _mic_handle_slot_transition (currently pending-generation discard):
        the outer tick must not overwrite the nested dispatch's RECOVERING
        marker even if its worker has already completed before the handler
        returns.
        """
        result = self._mic_slot.request_recovery(worker)
        if result == "dispatched":
            self._mic_last_observed_slot_state = audio_recovery.SlotState.RECOVERING.value
            self._mic_recovery_dispatch_serial += 1
        return result

    def _mic_quiesce_current_generation(self):
        """[P0] 1.3B2 -- detaches the failed hardware generation from the
        persistent quarantine queue and hands its NULL transition to a
        bounded background worker. The detach (pad unlink + Bin.remove)
        happens HERE, synchronously, on the GLib thread -- every physical
        round in scratchpad/audio_recovery/ treated this as fast/safe
        (never wrapped in a hang-watchdog); only the actual set_state()
        call against the now-orphaned bin is the proven hang risk (Round
        2's py-spy-confirmed pthread_mutex_lock contention), so only that
        goes through SlotCoordinator's background worker."""
        old_bin = self._mic_hw_bin
        self._mic_hw_bin = None
        if old_bin is None:
            return
        try:
            src_pad = old_bin.get_static_pad("src")
            sink_pad = self._mic_quarantine_q.get_static_pad("sink") if self._mic_quarantine_q else None
            if src_pad is not None and sink_pad is not None and src_pad.is_linked():
                src_pad.unlink(sink_pad)
            self._mic_bin.remove(old_bin)
        except Exception as exc:
            print(f"  Mic quiesce: failed to detach old generation cleanly ({exc}); "
                  f"still dispatching guarded teardown")

        def worker():
            old_bin.set_state(Gst.State.NULL)
            # Reaching this line without hanging is the whole point of
            # this operation -- Gst.StateChangeReturn is deliberately not
            # treated as pass/fail; an already-errored element returning
            # FAILURE from set_state(NULL) is still safe to abandon.
            return True

        result = self._mic_request_recovery(worker)
        print(f"  Mic quiesce dispatched: {result}")

    def _mic_dispatch_rebuild(self):
        """[P0] 1.3B2 -- called only from _mic_presence_probe_tick, only
        while the slot is DEGRADED (no operation in flight) and the
        configured stable identity is confirmed present. Construction and
        pad-linking happen here, synchronously (proven fast/non-hazardous
        throughout scratchpad/audio_recovery/); only the state-change call
        AND the bounded health-verification wait happen inside the
        background worker (see below) -- bundled together so
        SlotCoordinator's own success/failure semantics land exactly on
        this feature's locked health rule (PLAYING >= 500ms AND a fresh
        buffer <= 250ms), not merely on set_state() not hanging."""
        runtime_device = audio_recovery.resolve_runtime_device(
            self._mic_identity_kind, self._mic_identity, self._mic_legacy_device)
        if not runtime_device:
            return
        new_bin, new_src = self._build_mic_hw_generation(runtime_device)
        self._mic_bin.add(new_bin)
        new_bin.get_static_pad("src").link(self._mic_quarantine_q.get_static_pad("sink"))
        self._mic_pending_hw_bin = new_bin

        def worker():
            # PyGObject's sync_state_with_parent() returns a plain bool
            # (verified directly, not assumed -- it is NOT a
            # Gst.StateChangeReturn), so it's checked as one here.
            if not new_bin.sync_state_with_parent():
                return False
            # Locked health rule: PLAYING continuously for >= 500ms AND a
            # real buffer observed within the last 250ms. Only non-
            # blocking get_state(0) peeks (proven <120us,
            # scratchpad/audio_recovery/ Round 6) and plain attribute
            # reads -- nothing here can hang.
            playing_since = None
            deadline = time.monotonic() + 2.0  # generous grace for ASYNC preroll
            while time.monotonic() < deadline:
                _, st, _ = new_bin.get_state(0)
                if st == Gst.State.PLAYING:
                    if playing_since is None:
                        playing_since = time.monotonic()
                    elif time.monotonic() - playing_since >= 0.5:
                        break
                else:
                    playing_since = None
                time.sleep(0.05)
            if playing_since is None or (time.monotonic() - playing_since) < 0.5:
                return False
            age = self._mic_buffer_age_ms()
            return age is not None and age <= 250

        result = self._mic_request_recovery(worker)
        print(f"  Mic rebuild dispatched (generation {self._mic_hw_generation}): {result}")

    def _mic_discard_pending_hw_bin(self, abandoned=False):
        """A rebuild attempt did not pass health verification (or its
        state-change/health-wait was itself abandoned by the slot
        coordinator's timeout). Either way the pending bin is not
        promoted to self._mic_hw_bin.

        The detach (pad unlink + Bin.remove) happens HERE, synchronously,
        on the GLib thread -- empirically confirmed safe even against a
        genuinely wedged streaming thread (see [P0] 1.3B2 pre-commit
        review, Issue 2:
        scratchpad/audio_recovery/hotplug_harness/harness_r_detach_stream_lock_probe.py
        -- unlink()+Bin.remove() completed in <1ms while a synthetic
        wedge held the exact pad's stream lock; a negative control
        confirmed set_state(NULL) alone DOES hang under the identical
        condition, validating the test and confirming set_state() is the
        only real hazard here).

        On a plain (non-abandoned) failure, the NULL transition itself
        is dispatched through the SAME guarded background worker as
        _mic_quiesce_current_generation -- pre-commit review caught that
        an earlier draft called set_state(NULL) directly on the GLib
        thread here, which is exactly the synchronous-hardware-state-call
        pattern the locked design forbids (Round 2's original hang was
        specifically a NULL transition, not detach). Safe to dispatch:
        this method only runs while the slot is DEGRADED with no
        operation in flight (the prior rebuild op just resolved), so
        request_recovery() is guaranteed to accept it.

        On ABANDONMENT, per the still-open Round-3 question ("can a
        fresh element safely reopen the same device while an old,
        abandoned close() may still hold it?" -- untested, not assumed
        true), we do NOT attempt any further state call against this bin
        at all, guarded or not -- only detach it (proven safe above) so
        engine shutdown never has to wait on it, and then abandon the
        Python reference, exactly like the original object."""
        bin_obj = self._mic_pending_hw_bin
        self._mic_pending_hw_bin = None
        if bin_obj is None:
            return
        try:
            src_pad = bin_obj.get_static_pad("src")
            sink_pad = self._mic_quarantine_q.get_static_pad("sink") if self._mic_quarantine_q else None
            if src_pad is not None and sink_pad is not None and src_pad.is_linked():
                src_pad.unlink(sink_pad)
            self._mic_bin.remove(bin_obj)
        except Exception as exc:
            print(f"  Mic discard-pending: failed to detach cleanly ({exc})")
        if not abandoned:
            def worker():
                bin_obj.set_state(Gst.State.NULL)
                return True  # reaching this line without hanging is the point; see _mic_quiesce_current_generation's worker

            result = self._mic_request_recovery(worker)
            print(f"  Mic discard-pending NULL dispatched: {result}")

    def _mic_handle_slot_transition(self, old_state, new_state, snapshot):
        SlotState = audio_recovery.SlotState
        if new_state == SlotState.OK.value:
            if self._mic_pending_hw_bin is not None:
                # A REBUILD attempt succeeded (state-change + locked
                # health rule both passed, verified inside the worker).
                self._mic_hw_bin = self._mic_pending_hw_bin
                self._mic_pending_hw_bin = None
                self.mic_ok = True
                if self._mic_selector is not None and self._mic_device_pad is not None:
                    self._mic_selector.set_property("active-pad", self._mic_device_pad)
                # RECOVERED MIC SAFETY (locked): stays logically OFF.
                # Operator must explicitly key PTT again -- do NOT restore
                # mic_live/PTT volume here.
                self._mic_recovery_attempt = 0
                self._mic_next_retry_at = None
                self._mic_device_present = True
                print(f"  Mic recovered (generation {snapshot['generation']}) -- "
                      f"device audible again, PTT remains OFF")
                emit_event(
                    category="hardware", level="info", title="Studio microphone recovered",
                    detail={"generation": snapshot["generation"]},
                    dedupe_key=f"hardware|mic-recovered|gen{snapshot['generation']}",
                )
            else:
                # A QUIESCE succeeded (old generation torn down cleanly).
                # SlotCoordinator's own OK here means only "that operation
                # finished" -- the mic itself is not healthy yet (no
                # hardware generation exists at all right now). Re-mark
                # DEGRADED immediately so presence-probing continues to
                # gate a rebuild attempt; mic_ok/mic_live/PTT are already
                # OFF from _on_mic_error.
                print("  Mic old generation quiesced cleanly; waiting for device to return")
                self._mic_slot.mark_degraded()
        elif new_state == SlotState.DEGRADED.value:
            if self._mic_pending_hw_bin is not None:
                print("  Mic rebuild attempt failed health verification; will retry after backoff")
                self._mic_discard_pending_hw_bin(abandoned=False)
            elif snapshot.get("operation_succeeded") is False:
                # [P0] 1.3B4 -- distinguished via SlotCoordinator's own
                # operation_succeeded tag (added for exactly this purpose),
                # not inferred from incidental state. Found live during the
                # 1.3B3 physical acceptance test: the OK-branch's own
                # deliberate re-degrade below (mark_degraded() called again
                # right after a SUCCESSFUL quiesce) reuses the SAME
                # OperationRecord -- mark_degraded() never touches self._op
                # -- so a naive "pending_hw_bin is None -> must be a
                # failure" check couldn't tell that apart from a genuine
                # worker failure and printed this exact message on a
                # perfectly normal, successful transition. Now gated on
                # operation_succeeded being explicitly False (only true if
                # old_bin.set_state(Gst.State.NULL) itself raised -- no
                # scratchpad round ever observed this; only hanging was
                # observed, which goes through RESTART_REQUIRED instead).
                # self._mic_hw_bin was already cleared before dispatch, so
                # the old bin is harmlessly leaked (detached from _mic_bin,
                # can't block shutdown) rather than definitively NULL'd --
                # logged for visibility; presence-probing still proceeds
                # normally from here, since it only depends on slot state
                # being DEGRADED.
                print("  Mic quiesce operation failed unexpectedly (see exception in operation_failed "
                      "log above, if any); old generation reference abandoned, continuing")
            # else: operation_succeeded is True (the expected, intentional
            # re-degrade right below, after a successful quiesce) or None
            # (no operation has ever completed yet) -- neither is a
            # failure; nothing to log.
        elif new_state == SlotState.RESTART_REQUIRED.value:
            if self._mic_pending_hw_bin is not None:
                self._mic_discard_pending_hw_bin(abandoned=True)
            print("  Mic slot RESTART_REQUIRED -- a background hardware-state operation "
                  "was abandoned; no further automatic rebuild attempts for this process lifetime")
            emit_event(
                category="hardware", level="error", title="Studio microphone recovery abandoned",
                detail={
                    "generation": snapshot["generation"],
                    "note": "A background hardware-state operation did not complete within its "
                            "timeout and was abandoned. Automatic retry has stopped for this slot; "
                            "an engine restart is the only way to fully reclaim it.",
                },
                dedupe_key=f"hardware|mic-restart-required|gen{snapshot['generation']}",
            )

    def _mic_recovery_tick(self):
        """GLib timer, ~300ms -- non-blocking. Drives SlotCoordinator's
        own abandonment detection and reacts to any state transition it
        observes since the last tick. Deliberately does NOT touch
        GStreamer/Django state from inside the coordinator's background
        worker threads -- only from here, on the GLib thread, matching
        this file's established threading conventions."""
        if not self.running or self._mic_slot is None:
            return True
        self._mic_slot.tick()
        snapshot = self._mic_slot.snapshot()
        new_state = snapshot["state"]
        old_state = self._mic_last_observed_slot_state
        if new_state != old_state:
            dispatch_serial_before_handler = self._mic_recovery_dispatch_serial
            self._mic_handle_slot_transition(old_state, new_state, snapshot)
            if self._mic_recovery_dispatch_serial == dispatch_serial_before_handler:
                # No nested operation was dispatched while handling this
                # transition, so synchronize with the coordinator's actual
                # post-handler state. If the serial advanced,
                # _mic_request_recovery already installed the RECOVERING
                # marker that the next tick must process.
                self._mic_last_observed_slot_state = self._mic_slot.state.value
        return True

    def _mic_presence_probe_tick(self):
        """GLib timer, ~2s -- non-blocking (a plain /proc/asound/cards
        read, no subprocess -- see audio_recovery.read_alsa_cards_present).
        Only acts while the slot is DEGRADED with no operation in flight;
        RESTART_REQUIRED deliberately does not auto-retry (see
        SlotCoordinator/scratchpad Round 3's still-open question), OK
        needs nothing, RECOVERING/QUIESCING already has an op outstanding."""
        if not self.running or self._mic_slot is None:
            return True
        if self._mic_identity_kind != "alsa_card_id" or not self._mic_identity:
            # No stable identity configured for this input -- automatic
            # presence-based rebuild is deliberately not attempted (see
            # [P0] 1.3B2 report: "do not build hotplug recovery around a
            # raw numeric string if the returning device may enumerate at
            # a different card index"). Quiesce/silence-fallback above
            # still applies; only the auto-rebuild-on-return step needs
            # this identity.
            return True
        if self._mic_slot.state != audio_recovery.SlotState.DEGRADED:
            return True
        cards = audio_recovery.read_alsa_cards_present()
        present = audio_recovery.alsa_card_identity_present(self._mic_identity, cards)
        self._mic_device_present = present
        if not present:
            return True
        now = time.monotonic()
        if self._mic_next_retry_at is not None and now < self._mic_next_retry_at:
            return True
        self._mic_recovery_attempt += 1
        self._mic_next_retry_at = now + audio_recovery.compute_backoff_seconds(self._mic_recovery_attempt)
        self._mic_dispatch_rebuild()
        return True

    def _mic_recovery_state(self):
        """[P0] 1.3B2 -- engine_state.json's mic_recovery block. None
        when no mic is configured at all (matches mic_configured=False;
        there's genuinely nothing to report)."""
        if self._mic_slot is None:
            return None
        snapshot = self._mic_slot.snapshot()
        next_retry_in_s = None
        if self._mic_next_retry_at is not None:
            # next_retry_in_s, not a raw monotonic timestamp -- monotonic
            # clock epoch is meaningless to a JSON consumer/dashboard;
            # "seconds from now" is directly usable, matching timestamp's
            # own wall-clock (time.time()) convention elsewhere in this
            # payload.
            next_retry_in_s = max(0.0, round(self._mic_next_retry_at - time.monotonic(), 1))
        return {
            "state": snapshot["state"],
            "generation": snapshot["generation"],
            "device_present": self._mic_device_present,
            "runtime_device": audio_recovery.resolve_runtime_device(
                self._mic_identity_kind, self._mic_identity, self._mic_legacy_device),
            "last_error": self._mic_last_error,
            "last_state_change": self._mic_last_state_change_at,
            "next_retry_in_s": next_retry_in_s,
            "restart_required": snapshot["state"] == audio_recovery.SlotState.RESTART_REQUIRED.value,
        }

    # ------------------------------------------------------------------
    # [P0] 1.3C -- audio OUTPUT device hotplug recovery. Mirrors the
    # mic-recovery methods above wherever the shape genuinely matches
    # (_on_mic_error -> _on_output_error, _mic_quiesce_current_generation
    # -> _output_quiesce_current_generation, etc.), parameterized by an
    # OutputRecoverySlot instead of flat self._mic_* attributes since
    # there are two independent slots (Studio Monitor, Stereotool Input),
    # not one. See scratchpad/audio_output_recovery/
    # ROUND7_DECISION_REPORT.md for the discovery/proof this is based on.
    # ------------------------------------------------------------------

    def _resolve_output_device_identity(self, name):
        """Returns (identity_kind, identity) for the AudioOutput row
        named `name` -- the stable-identity half only. The legacy/
        fallback device string itself is still resolved by
        _resolve_studio_monitor_device/_resolve_stereotool_device
        (left completely unchanged) so this doesn't duplicate or risk
        diverging from their existing fallback-device behavior; callers
        combine the two. Mirrors _resolve_mic_identity's read/log
        pattern exactly."""
        try:
            row = AudioOutput.objects.filter(name=name).values(
                "device_identity_kind", "device_identity").first()
        except Exception as exc:
            print(f"  Failed to read AudioOutput identity config for '{name}' ({exc}); automatic recovery disabled")
            return "", ""
        if not row:
            return "", ""
        identity_kind = row.get("device_identity_kind") or ""
        identity = row.get("device_identity") or ""
        if identity_kind == "alsa_card_id" and identity:
            print(f"  AudioOutput '{name}' automatic recovery enabled (alsa_card_id={identity!r})")
        else:
            print(f"  AudioOutput '{name}' has no stable device identity configured; "
                  f"automatic rebuild-on-return disabled (containment still applies)")
        return identity_kind, identity

    def _reload_output_recovery_identity(self):
        """[P0] 1.3C -- pre-commit review finding: an operator setting
        device_identity_kind/device_identity in the admin for the first
        time (enabling automatic recovery) had no live-reload path at
        all -- self._studio_monitor_slot/self._stereotool_slot's
        identity_kind/identity were read ONCE at _build_main_pipeline
        time and never refreshed, so the admin would silently claim
        "Automatic Recovery enabled" while the running engine kept
        using the stale (usually blank) values until the next full
        restart. This command fixes that -- see hardware/signals.py's
        post_save handler, which now fires it on every AudioOutput
        save, and _check_commands' "reload_audio_output"/
        "reload_audio_output_recovery_config" handlers below.

        Refreshes identity_kind/identity for every built output slot and
        returns the kinds whose identity changed. The caller treats a
        Studio Monitor identity change as an explicit branch-local
        retarget; other output identities remain metadata-only live reloads.
        This method itself never touches a generation, valve, or raw path.

        Iterates every BUILT slot regardless of which specific
        AudioOutput row was just saved (cheap, idempotent DB reads) --
        simpler and just as correct as trying to track exactly which
        row triggered the call. Safe to call with zero slots built
        (e.g. before _build_main_pipeline has ever run) -- no-op."""
        changed_kinds = set()
        for slot in self._output_slots.values():
            identity_kind, identity = self._resolve_output_device_identity(slot.name)
            if (identity_kind, identity) != (slot.identity_kind, slot.identity):
                print(f"  Output recovery identity reload ({slot.name}): "
                      f"identity_kind {slot.identity_kind!r}->{identity_kind!r}, "
                      f"identity {slot.identity!r}->{identity!r}")
                slot.identity_kind = identity_kind
                slot.identity = identity
                changed_kinds.add(slot.kind)
        return changed_kinds

    def _build_output_containment_queue(self, name):
        """[P0] 1.3C -- persistent, leaky-upstream queue for one
        physical-output branch off stereotool_tee (or directly off
        output_level for Studio Monitor when StereoTool isn't
        configured). Round 4's own empirical finding
        (scratchpad/audio_output_recovery/round4_blocking_containment/):
        a queue only GUARANTEES it can never become the thing that
        blocks tee's shared push if it's leaky -- a bounded NON-leaky
        queue still fills up and blocks upstream once its capacity is
        exhausted, just with a longer runway than no queue at all,
        which would eventually reproduce the exact sibling-stall hazard
        this whole design exists to prevent (Round 6's real UCA222 test
        needed a 46-SECOND device-absent window; nothing drains a
        non-leaky queue for that long).

        Leaky is safe here specifically because it costs almost
        nothing extra: once this branch's OWN valve closes (near-
        instant per Round 5/6), the valve absorbs everything pushed
        into it with zero backpressure regardless of queue policy -- so
        leaky policy mainly matters for the rarer "hangs without ever
        erroring, valve never closes automatically" case Round 4
        identified, where it's the ONLY thing standing between a
        wedged branch and a stalled shared pipeline.

        Unlike a naive "just match StereoTool" copy, this reasoning
        applies EQUALLY to Studio Monitor: dropping ITS OWN queued
        audio during ITS OWN hardware outage costs nothing beyond what
        is already lost (nothing is being rendered to broken/absent
        hardware either way) -- there is no real "primary output
        deserves non-leaky" tradeoff being made here, so both branches
        use the same policy for the same reason, not by default or
        laziness. Same size config as the pre-1.3C stereotool_queue
        (1s / unbounded-buffers / unbounded-bytes) -- no evidence
        pointed at a different size being needed for either branch."""
        queue = Gst.ElementFactory.make("queue", name)
        queue.set_property("leaky", 1)  # upstream -- drop new buffers once full, never block the push in
        queue.set_property("max-size-time", 1_000_000_000)  # 1s
        queue.set_property("max-size-buffers", 0)
        queue.set_property("max-size-bytes", 0)
        return queue

    def _build_output_errorignore(self, name):
        """[P0] 1.3C -- explicit configuration, never relying on plugin
        defaults (Round 1 found the defaults are ignore-error=True but
        ALSO ignore-notnegotiated=True, which is deliberately NOT
        wanted here -- a caps/negotiation bug is a real programming/
        configuration problem and must stay visible/fatal, not silently
        masked). convert-to=ok (not the default 'not-linked') -- Round
        3's own supplementary finding: 'ok' keeps this branch's queue
        fed at the normal rate indefinitely even while the downstream
        generation is failing, the most predictable foundation for the
        valve/rebuild choreography below; the default leaves the
        branch's own queue silently starved instead."""
        errignore = Gst.ElementFactory.make("errorignore", name)
        errignore.set_property("ignore-error", True)
        errignore.set_property("ignore-notnegotiated", False)
        errignore.set_property("convert-to", 0)  # GST_FLOW_OK
        return errignore

    def _build_studio_monitor_hw_generation(self, device, *, sink_factory="alsasink"):
        """Build the complete replaceable Studio Monitor branch tail.

        The generation contains AGC, makeup, limiter, and sink. Physical
        Gate A testing found a sink-only replacement could report PLAYING,
        render buffers, and advance ALSA hw_ptr while the persistent
        processing leg emitted digital zero. Keeping these stateful
        processors inside the same bounded generation means an intentional
        retarget resets the whole Studio-Monitor-local tail without touching
        the parent pipeline or StereoTool sibling. The containment queue,
        errorignore, and valve remain persistent outside this bin.

        async=False remains required because recovery links the new
        generation behind a closed valve and waits for PLAYING before opening
        it; an asynchronous base sink would wait forever for a preroll buffer
        that the closed valve cannot provide. sync=False is also deliberate:
        boundary-level physical capture on two UCA222s reproduced the silent
        Studio output with clean nonzero PCM at the sink pad and advancing
        ALSA hw_ptr when clock scheduling was enabled; changing only sync to
        False restored the captured signal on both devices. This live-source
        branch is paced upstream, and its valve consumes while closed, so
        disabling sink scheduling cannot flush a retained queue backlog.

        sink_factory is injectable only for hardware-free regression
        coverage; production always uses the default alsasink.
        """
        bin_ = Gst.Bin.new(f"studio_monitor_gen{int(time.time() * 1000)}")
        dynamic = Gst.ElementFactory.make("audiodynamic", "agc_dynamic")
        makeup = Gst.ElementFactory.make("volume", "agc_makeup")
        limiter = Gst.ElementFactory.make("rglimiter", "agc_limiter")
        sink = Gst.ElementFactory.make(sink_factory, None)
        if sink_factory == "alsasink":
            sink.set_property("device", device)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        for element in (dynamic, makeup, limiter, sink):
            bin_.add(element)
        dynamic.link(makeup)
        makeup.link(limiter)
        limiter.link(sink)
        self._configure_studio_monitor_generation(bin_)
        ghost = Gst.GhostPad.new("sink", dynamic.get_static_pad("sink"))
        ghost.set_active(True)
        bin_.add_pad(ghost)
        return bin_, sink

    def _configure_studio_monitor_generation(self, bin_, settings=None):
        """Apply cached/live AGC settings to one disposable generation."""
        settings = settings or getattr(self, "_studio_monitor_agc_settings", None)
        settings = settings or {
            "enabled": False,
            "ratio": 1.0,
            "threshold": 0.0,
            "soft_knee": False,
            "makeup_gain_db": 0.0,
        }
        dynamic = bin_.get_by_name("agc_dynamic")
        makeup = bin_.get_by_name("agc_makeup")
        limiter = bin_.get_by_name("agc_limiter")
        if not all((dynamic, makeup, limiter)):
            return
        if settings["enabled"]:
            dynamic.set_property("ratio", settings["ratio"])
            dynamic.set_property("threshold", settings["threshold"])
            dynamic.set_property("characteristics", 1 if settings["soft_knee"] else 0)
            makeup.set_property("volume", 10 ** (settings["makeup_gain_db"] / 20.0))
            limiter.set_property("enabled", True)
        else:
            dynamic.set_property("ratio", 1.0)
            makeup.set_property("volume", 1.0)
            limiter.set_property("enabled", False)

    def _build_stereotool_hw_generation(self, device):
        """[P0] 1.3C -- same shape as
        _build_studio_monitor_hw_generation, but reapplies StereoTool's
        special sink properties on EVERY generation (not just the
        first) -- these are not defaults, they were tuned live against
        real click/dropout symptoms (see the pre-1.3C construction
        site's own extensive comments, unchanged reasoning, now living
        here) and must survive every rebuild identically."""
        bin_ = Gst.Bin.new(f"stereotool_gen{int(time.time() * 1000)}")
        sink = Gst.ElementFactory.make("alsasink", None)
        sink.set_property("device", device)
        # sync=False/async=False -- without async=False, this sink's own
        # preroll (waiting for its first buffer) can block the entire
        # containing bin's PAUSED -> PLAYING transition if the leaky
        # queue upstream ever drops that first buffer -- verified this
        # stalls a real pipeline (Round 4 independently re-confirmed the
        # identical hazard for a different sink). sync=False means it
        # just renders buffers as they arrive rather than clock-pacing
        # them against Studio Monitor's own hardware clock.
        sink.set_property("sync", False)
        sink.set_property("async", False)
        # Enlarged ALSA ring on this sink specifically -- see the
        # pre-1.3C construction site's history (clicks were heard on the
        # FM output/streams downstream of StereoTool but NOT on the
        # studio monitor, localizing the fault to this loopback-fed
        # segment; kernel xrun counters were zero at the time, consistent
        # with StereoTool covering brief input starves rather than
        # surfacing an error). buffer-time=200000 (~200ms, vs. the ~43ms
        # driver default) gives ~4.6x more runway before a scheduler
        # stall starves the read side; latency-time=20000 keeps ALSA
        # waking the writer roughly every 20ms. ~160ms extra one-way
        # latency, inconsequential for a broadcast chain -- Studio
        # Monitor's own generation is unaffected (driver default).
        sink.set_property("buffer-time", 200000)
        sink.set_property("latency-time", 20000)
        bin_.add(sink)
        ghost = Gst.GhostPad.new("sink", sink.get_static_pad("sink"))
        ghost.set_active(True)
        bin_.add_pad(ghost)
        return bin_, sink

    def _build_output_slot(self, name, kind, legacy_device, build_generation_fn):
        """[P0] 1.3C -- builds one complete OutputRecoverySlot: persistent
        queue + errorignore + valve, plus a first hardware generation
        built synchronously here at cold start (same risk/timing profile
        as every other device construction in this file -- a bad/absent
        device still surfaces via the normal async bus-error path a
        moment after PLAYING; see _build_mic_chain's own docstring for
        the identical precedent, and this phase's own startup-behavior
        investigation in the final report)."""
        identity_kind, identity = self._resolve_output_device_identity(name)
        runtime_device = audio_recovery.resolve_runtime_device(identity_kind, identity, legacy_device)
        queue = self._build_output_containment_queue(f"{kind}_queue")
        errignore = self._build_output_errorignore(f"{kind}_errorignore")
        valve = Gst.ElementFactory.make("valve", f"{kind}_valve")
        valve.set_property("drop", False)
        bin_, sink = build_generation_fn(runtime_device)
        return OutputRecoverySlot(
            name=name, kind=kind, queue=queue, errorignore=errignore, valve=valve,
            build_generation_fn=build_generation_fn,
            legacy_device=legacy_device, identity_kind=identity_kind, identity=identity,
            current_bin=bin_, current_sink=sink,
        )

    def _on_output_error(self, slot, bus, message):
        """[P0] 1.3C -- mirrors _on_mic_error's structure, scoped to ONE
        output slot. Unlike mic (which reacts by switching to silence),
        an output branch's containment is already in place BEFORE any
        failure via errorignore (Round 3) -- the only new action here is
        closing THIS slot's OWN valve, which stops further buffers from
        entering the (about to be torn down) failed generation. The
        healthy sibling slot and the shared upstream tee/pipeline are
        never touched (steps 5-8 of the locked device-loss sequence)."""
        err, debug = message.parse_error()
        print(f"  Output error ({slot.name}): {err} ({debug})")
        slot.last_error = str(err)[:300]
        slot.last_state_change_at = time.time()

        classification = audio_recovery.classify_audio_device_error(str(err), str(debug))
        if classification != "device_lost":
            print(f"  Output error ({slot.name}) classified '{classification}' -- logged only, no recovery action")
            return True

        # Bump the device-loss epoch UNCONDITIONALLY, before checking
        # mark_degraded()'s return value -- pre-commit review finding:
        # a candidate generation being health-verified (valve open,
        # rendered count already increasing) could still be promoted
        # "recovered" even though a FRESH device-loss arrived mid-
        # verification, because mark_degraded() correctly returns False
        # for it (the slot is already RECOVERING, not OK) and the
        # verifying worker's own success check had already latched
        # rendered_increased=True by that point. This counter is the
        # fix -- see _output_dispatch_rebuild's worker for the other
        # half. Bumped even for a coalesced repeat (mark_degraded()
        # returning False) precisely because THIS is the case that
        # matters most: a repeat failure arriving while a rebuild is
        # already in flight for that same generation.
        slot.record_device_loss()

        # Close the valve UNCONDITIONALLY, every time this fires -- even
        # a coalesced repeat for the same already-degraded generation
        # (a real unplug produced a 62-message burst within 11.6ms in
        # Round 6's physical testing; the valve must already be shut
        # well before the 2nd..62nd message arrives), and even a fresh
        # error arriving from a PENDING (being health-verified)
        # generation -- belt-and-braces: the verifying worker also
        # closes the valve itself on a failed rendered-count check (see
        # _output_dispatch_rebuild), but a real bus error arriving
        # mid-verification should never rely on timing between two
        # independent code paths to keep the valve shut.
        slot.valve.set_property("drop", True)

        first_failure = slot.coordinator.mark_degraded()
        if not first_failure:
            return True

        print(f"  Output slot DEGRADED ({slot.name}, generation {slot.coordinator.generation})")
        slot.recovery_attempt = 0
        slot.next_retry_at = None
        slot.device_present = None
        slot.loss_episode += 1
        # [P0] 1.3C physical-acceptance-failure fix -- the dedupe key is
        # now STABLE per slot (no coordinator generation in it). Physical
        # UCA222 testing surfaced a real production event flood: a
        # separate bug (see _output_quiesce_current_generation's own
        # fix, below) let a burst of stale bus errors from an already-
        # detached generation keep re-triggering genuine OK->DEGRADED
        # transitions, each bumping SlotCoordinator.generation -- and
        # since THAT counter was baked into this dedupe key, every one
        # of ~560 events in ~2.5s defeated emit_event's normal 60-second
        # coalescing and became its own dashboard row. Fixed at the root
        # (that storm can no longer happen at all), but hardening this
        # key is deliberate defense-in-depth: coordinator.generation is
        # an internal operation-tagging counter, never a UI spam
        # boundary -- if some future bug reintroduces rapid flapping,
        # repeats now coalesce into repeat_count on ONE row instead of
        # flooding the dashboard again. generation and the separate,
        # UI-facing loss_episode counter (bumped once per genuine
        # OK->DEGRADED transition, independent of SlotCoordinator) both
        # move into `detail` instead, so operators can still see how
        # many distinct episodes a coalesced row represents.
        emit_event(
            category="hardware", level="warning", title=f"{slot.name} output lost",
            detail={"error": slot.last_error, "generation": slot.coordinator.generation,
                    "loss_episode": slot.loss_episode},
            dedupe_key=f"hardware|output-lost|{slot.kind}",
        )
        self._output_quiesce_current_generation(slot)
        return True

    def _output_request_recovery(self, slot, worker):
        """[P0] 1.3C physical-acceptance-failure fix -- thin wrapper
        around slot.coordinator.request_recovery(worker), shared by all
        three output dispatch sites (current-generation quiesce,
        candidate rebuild, pending-generation discard). Closes the
        polling-observer race: _output_recovery_tick only calls
        _output_handle_slot_transition when snapshot["state"] differs
        from slot.last_observed_slot_state, polled every ~300ms -- but a
        real UCA222 teardown was observed completing in well under that,
        meaning OK->DEGRADED->RECOVERING->OK could all happen between
        two ticks, leaving last_observed_slot_state sitting at "OK"
        throughout and silently skipping the transition handler entirely
        (no re-degrade, no promotion bookkeeping -- exactly the kind of
        gap that let stale bus errors alone look like they were driving
        the whole gen0->gen563 storm).

        request_recovery() sets state to RECOVERING, synchronously,
        under its own lock, BEFORE returning "dispatched" (see
        SlotCoordinator._dispatch_locked) -- and the worker thread's own
        eventual _on_worker_resolved() call needs that SAME lock, so it
        cannot race ahead of this call's return. That makes "the
        coordinator is in RECOVERING right now" a fact, not a guess, at
        the exact moment "dispatched" comes back -- so recording it into
        last_observed_slot_state HERE, synchronously, on the GLib
        thread, is safe and race-free. The very next tick then always
        has a true baseline to diff against, however fast the worker
        resolves -- timing correctness no longer depends on a 300ms poll
        catching an intermediate state.

        Deliberately narrow: only "dispatched" means a NEW transition to
        RECOVERING just happened. "coalesced" means the coordinator was
        ALREADY RECOVERING (last_observed_slot_state should already
        reflect that from whichever dispatch got coalesced onto).
        "ignored_ok"/"ignored_restart_required" mean nothing changed at
        all. Updating on anything else would be recording something that
        didn't actually happen.

        [P0] 1.3C SECOND physical-acceptance-failure fix (review round
        2) -- also bumps slot.recovery_dispatch_serial on "dispatched",
        synchronously, same call, same thread, same guarantee as the
        last_observed_slot_state write above. This is the marker
        _output_recovery_tick's post-handler bookkeeping now checks
        before it's allowed to overwrite last_observed_slot_state at
        all -- closing a SECOND, narrower race the first fix's own
        "re-read coordinator.state fresh after the handler returns"
        approach still had: if a NESTED operation dispatched from
        inside _output_handle_slot_transition (e.g. discard-pending
        firing from the DEGRADED branch) resolves all the way back to
        OK before that handler call even returns, re-reading
        coordinator.state fresh sees "OK" and erases the RECOVERING
        marker this method just installed -- even though the marker was
        correct and the tick that installed it hasn't had a chance to
        observe it yet. A value comparison against coordinator.state
        can't distinguish "nothing new was dispatched" from "something
        new was dispatched AND already finished" -- both look like "the
        state moved on" from the same starting point. The serial can:
        it only advances on an actual NEW dispatch, so the tick can tell
        the two cases apart deterministically, never by hoping a
        millisecond-scale NULL worker (physical UCA222-confirmed) loses
        the race."""
        result = slot.coordinator.request_recovery(worker)
        if result == "dispatched":
            slot.last_observed_slot_state = audio_recovery.SlotState.RECOVERING.value
            slot.recovery_dispatch_serial += 1
        return result

    def _output_quiesce_current_generation(self, slot):
        """[P0] 1.3C -- mirrors _mic_quiesce_current_generation exactly:
        detach (unlink + Bin.remove) happens HERE, synchronously, on the
        GLib thread -- proven fast/safe; only the actual
        set_state(NULL) call against the now-orphaned bin goes through
        the guarded background worker (the proven hang risk, per the
        original mic-recovery investigation's own py-spy-confirmed
        pthread_mutex_lock contention finding).

        [P0] 1.3C physical-acceptance-failure fix -- slot.current_bin/
        slot.current_sink are now retired (set to None) HERE,
        immediately after the detach, BEFORE request_recovery() even
        dispatches the NULL worker. Previously they kept pointing at
        this now-unlinked-and-removed old generation all the way until
        (if ever) a FUTURE rebuild got promoted -- so
        _output_slot_owns_message_src kept matching the remaining ALSA
        error burst from that detached generation against THIS slot,
        long after it stopped being the slot's real current generation.
        Confirmed exactly this in physical UCA222 testing: a real unplug
        produces a burst of near-simultaneous bus errors (Round 6's own
        62-message/11.6ms precedent, and now ~560 in ~2.5s), and every
        one after the first was still being attributed here, each
        landing on a coordinator that had ALREADY cycled back to OK
        (see _output_request_recovery's docstring for the other half of
        why that kept happening so fast) -- re-triggering mark_degraded()
        again and again, a fresh quiesce every time, generation climbing
        without bound. old_bin is kept as a local closure variable for
        the worker below (never re-read from the slot), so the teardown
        itself is completely unaffected by retiring the slot's own
        reference to it."""
        old_bin = slot.current_bin
        valve_src = slot.valve.get_static_pad("src")
        try:
            old_sink_pad = old_bin.get_static_pad("sink")
            if old_sink_pad is not None and valve_src.is_linked():
                valve_src.unlink(old_sink_pad)
            self.main_pipeline.remove(old_bin)
        except Exception as exc:
            print(f"  Output quiesce ({slot.name}): failed to detach old generation cleanly ({exc}); "
                  f"still dispatching guarded teardown")
        # Retired regardless of whether the try above hit its except --
        # even a failed/partial detach means this generation is no
        # longer something later code should treat as "the healthy
        # current sink" (mirrors the same all-paths-retire treatment
        # _output_discard_pending_bin already gives pending_bin/
        # pending_sink, just one line later here since old_bin has to be
        # captured first).
        slot.current_bin = None
        slot.current_sink = None

        def worker():
            old_bin.set_state(Gst.State.NULL)
            # Reaching this line without hanging is the whole point --
            # see _mic_quiesce_current_generation's identical comment.
            return True

        result = self._output_request_recovery(slot, worker)
        print(f"  Output quiesce dispatched ({slot.name}): {result}")

    def _output_dispatch_rebuild(self, slot):
        """[P0] 1.3C -- mirrors _mic_dispatch_rebuild's construction/
        link-synchronous, state-change+health-wait-in-background split.

        Health rule (named constants, measured against real Round 6
        hardware evidence, not assumed): the fresh generation must reach
        PLAYING and hold it for OUTPUT_HEALTH_STABILIZATION_S, AND (once
        the valve is reopened for verification) GstBaseSink
        stats()["rendered"] must actually increase within
        OUTPUT_RENDER_VERIFY_DEADLINE_S -- PLAYING alone is not proof,
        matching the locked mic-recovery precedent (and directly
        motivated by Round 6's own physical finding: a real UCA222
        rebuild reached PLAYING via sync_state_with_parent() while
        genuinely stuck on a preroll wait).

        Valve choreography (the exact ordering proven in Round 5/6, now
        formalized as a hard gate rather than just observed after the
        fact): the valve stays CLOSED while only the state-based check
        has passed, opens ONLY once PLAYING has stabilized, and -- if
        rendering doesn't materialize within the verification window --
        closes again and this attempt is reported as failed (never left
        half-open), before the next presence-probe-triggered attempt
        discards it and tries a genuinely fresh generation. All of this
        runs on the worker's own background thread -- property sets and
        get_state(0)/stats reads are safe from any thread, only a real
        hardware set_state() call is the proven hazard being kept off
        the GLib thread.

        Pre-commit review finding, fixed here: PLAYING-hold + rendered-
        count-increase alone are NOT sufficient to declare success --
        a device_lost error can arrive for THIS generation after
        rendered_increased already latched True but before the worker
        formally returns, and since SlotCoordinator.mark_degraded()
        correctly returns False for that (the slot is already
        RECOVERING, not OK -- see _on_output_error), nothing would
        otherwise stop this worker from still returning True and
        getting promoted "recovered" moments after a fresh real failure.
        slot.device_loss_epoch() is captured HERE, before dispatch, and
        checked again immediately before the worker returns True -- if
        it changed, a device-loss happened somewhere in this whole
        window (state-check, valve-open, or render-verify) and this
        attempt must fail, valve closed, no promotion, matching the
        locked health requirement's third clause exactly.

        SECOND pre-commit review finding, also fixed here: the worker's
        own check above closes the race for anything happening WHILE
        the worker is still running, but there is a further, narrower
        TOCTOU window between the worker returning True and
        SlotCoordinator's runner() actually flipping state to OK (not
        atomic with the GLib thread's own locking), plus the gap until
        _output_recovery_tick's next ~300ms poll notices that
        transition at all. The SAME captured epoch value is stashed on
        slot.pending_validation_epoch (persistent, since the SECOND
        check lives in a different method, _output_handle_slot_
        transition, invoked later/elsewhere) -- see that method's OK-
        branch for the promotion-time re-check that closes this
        narrower window without needing to touch SlotCoordinator at all."""
        runtime_device = audio_recovery.resolve_runtime_device(slot.identity_kind, slot.identity, slot.legacy_device)
        if not runtime_device:
            return
        failure_epoch_at_start = slot.device_loss_epoch()
        new_bin, new_sink = slot.build_generation_fn(runtime_device)
        self.main_pipeline.add(new_bin)
        slot.valve.get_static_pad("src").link(new_bin.get_static_pad("sink"))
        slot.pending_bin = new_bin
        slot.pending_sink = new_sink
        slot.pending_validation_epoch = failure_epoch_at_start

        def worker():
            if not new_bin.sync_state_with_parent():
                return False
            playing_since = None
            deadline = time.monotonic() + OUTPUT_HEALTH_CHECK_DEADLINE_S
            while time.monotonic() < deadline:
                _, st, _ = new_bin.get_state(0)
                if st == Gst.State.PLAYING:
                    if playing_since is None:
                        playing_since = time.monotonic()
                    elif time.monotonic() - playing_since >= OUTPUT_HEALTH_STABILIZATION_S:
                        break
                else:
                    playing_since = None
                time.sleep(0.05)
            if playing_since is None or (time.monotonic() - playing_since) < OUTPUT_HEALTH_STABILIZATION_S:
                return False

            # State-only check passed. Open the valve to let real
            # buffers actually reach this generation -- required to
            # prove rendering at all -- then verify, then close again
            # on failure so a bad generation is never left half-promoted.
            rendered_before = _output_sink_rendered_count(new_sink)
            slot.valve.set_property("drop", False)
            verify_deadline = time.monotonic() + OUTPUT_RENDER_VERIFY_DEADLINE_S
            rendered_increased = False
            while time.monotonic() < verify_deadline:
                rendered_now = _output_sink_rendered_count(new_sink)
                if rendered_before is not None and rendered_now is not None and rendered_now > rendered_before:
                    rendered_increased = True
                    break
                # Bail out early the moment a fresh device-loss is
                # observed, rather than waiting out the rest of the
                # verify window -- purely a responsiveness optimization,
                # the authoritative gate is the check right before
                # returning True below regardless.
                if slot.device_loss_epoch() != failure_epoch_at_start:
                    break
                time.sleep(0.05)
            if not rendered_increased:
                slot.valve.set_property("drop", True)
                return False

            # Authoritative epoch gate -- checked AGAIN here, right
            # before declaring success, even though rendered_increased
            # is already True. A device-loss landing in the narrow
            # window between the render-verify loop's last check and
            # this line is exactly the race this whole mechanism exists
            # to close.
            if slot.device_loss_epoch() != failure_epoch_at_start:
                slot.valve.set_property("drop", True)
                return False
            return True

        result = self._output_request_recovery(slot, worker)
        print(f"  Output rebuild dispatched ({slot.name}, generation {slot.coordinator.generation}): {result}")

    def _output_discard_pending_bin(self, slot, abandoned=False):
        """[P0] 1.3C -- mirrors _mic_discard_pending_hw_bin exactly,
        including the abandonment split (detach always safe/synchronous;
        the NULL state-change is guarded UNLESS abandoned, in which case
        the bin is never touched again at all -- same still-open
        question this codebase already declines to resolve for mic:
        can a fresh element safely reopen the same device while an old,
        abandoned close() may still hold it? Untested, not assumed
        true).

        [P0] 1.3C physical-acceptance-failure fix, item 2 audit result:
        slot.pending_bin/slot.pending_sink were ALREADY cleared to None
        right here, at the very top, well before any detach or worker
        dispatch -- so a stale bus error from a discarded candidate was
        already correctly unable to match _output_slot_owns_message_src
        (which reads slot.pending_bin fresh on every call) before this
        physical-testing round ever ran. No ownership-fix needed for
        THIS half of Bug 1 -- see test_stale_pending_generation_error_
        ignored for the regression test locking it in. The observer-race
        fix (self._output_request_recovery) still applies here the same
        as the other two dispatch sites."""
        bin_obj = slot.pending_bin
        slot.pending_bin = None
        slot.pending_sink = None
        slot.pending_validation_epoch = None
        if bin_obj is None:
            return
        try:
            sink_pad = bin_obj.get_static_pad("sink")
            valve_src = slot.valve.get_static_pad("src")
            if sink_pad is not None and valve_src.is_linked():
                valve_src.unlink(sink_pad)
            self.main_pipeline.remove(bin_obj)
        except Exception as exc:
            print(f"  Output discard-pending ({slot.name}): failed to detach cleanly ({exc})")
        if not abandoned:
            def worker():
                bin_obj.set_state(Gst.State.NULL)
                return True

            result = self._output_request_recovery(slot, worker)
            print(f"  Output discard-pending NULL dispatched ({slot.name}): {result}")

    def _output_handle_slot_transition(self, slot, old_state, new_state, snapshot):
        """[P0] 1.3C -- mirrors _mic_handle_slot_transition exactly,
        including the [P0] 1.3B4 operation_succeeded-gated diagnostic
        fix (distinguishing a deliberate re-degrade-after-successful-
        quiesce from a genuine worker failure).

        SECOND pre-commit review finding, fixed here: the worker's own
        epoch check (see _output_dispatch_rebuild) closes the race for
        anything that happens WHILE the worker is still running, but
        NOT the narrower TOCTOU window between the worker returning
        True and this method actually running to process the
        resulting OK transition (SlotCoordinator's own runner()
        flipping state to OK is not atomic with a concurrent GLib-
        thread bus-error handler's locking, and this method itself
        only runs on _output_recovery_tick's ~300ms poll, not
        instantly). A device-loss landing in EITHER sub-window bumps
        slot.device_loss_epoch() while the coordinator was still
        RECOVERING -- correctly coalesced there (SlotCoordinator's own
        unmodified contract), and therefore invisible to it -- so this
        promotion-time re-check, immediately below, is the only place
        left that can still catch it before pending_bin is promoted."""
        SlotState = audio_recovery.SlotState
        if new_state == SlotState.OK.value:
            if slot.pending_bin is not None:
                current_epoch = slot.device_loss_epoch()
                if current_epoch != slot.pending_validation_epoch:
                    # A device-loss arrived after the worker's own final
                    # check passed but before this promotion-time check
                    # ran -- do NOT promote. The coordinator has already
                    # reached OK (that's why we're in this branch at
                    # all), so mark_degraded() is guaranteed to succeed
                    # here (unlike the worker-side attempt at the same
                    # notification, which was correctly coalesced while
                    # still RECOVERING) -- this is what actually performs
                    # the OK -> DEGRADED transition for that missed
                    # notification, deterministically, rather than
                    # depending on a bus-error handler having won a race
                    # for SlotCoordinator's own lock. The failed
                    # candidate is discarded via the SAME bounded
                    # machinery the ordinary "rebuild failed health
                    # verification" DEGRADED-branch below already uses --
                    # correctly scoped to slot.pending_bin (the actual
                    # failed candidate), not slot.current_bin (whatever
                    # stale/already-detached reference that still holds
                    # until a promotion actually happens).
                    print(f"  Output ({slot.name}) device-loss detected between worker success and "
                          f"promotion (epoch {slot.pending_validation_epoch} -> {current_epoch}); "
                          f"discarding candidate, not promoting")
                    slot.valve.set_property("drop", True)
                    slot.coordinator.mark_degraded()
                    self._output_discard_pending_bin(slot, abandoned=False)
                    return
                # A REBUILD attempt succeeded -- state-change, PLAYING-
                # hold, valve-reopen, AND rendered-count verification all
                # already happened and passed inside the worker itself
                # (see _output_dispatch_rebuild); the valve is already
                # open. Promotion here is pure bookkeeping.
                intentional_retarget = slot.retarget_in_progress
                slot.current_bin = slot.pending_bin
                slot.current_sink = slot.pending_sink
                slot.pending_bin = None
                slot.pending_sink = None
                slot.pending_validation_epoch = None
                slot.recovery_attempt = 0
                slot.next_retry_at = None
                slot.device_present = True
                slot.retarget_in_progress = False
                if intentional_retarget:
                    print(f"  Output retarget completed ({slot.name}, generation "
                          f"{snapshot['generation']}) -- requested device rendering")
                else:
                    print(f"  Output recovered ({slot.name}, generation {snapshot['generation']}) -- "
                          f"device rendering again")
                    emit_event(
                        category="hardware", level="info", title=f"{slot.name} output recovered",
                        detail={"generation": snapshot["generation"], "loss_episode": slot.loss_episode},
                        dedupe_key=f"hardware|output-recovered|{slot.kind}",
                    )
            else:
                # A QUIESCE succeeded (old generation torn down cleanly).
                # SlotCoordinator's own OK here means only "that
                # operation finished" -- nothing is rendering yet. Re-
                # mark DEGRADED immediately so presence-probing continues
                # to gate a rebuild attempt.
                print(f"  Output ({slot.name}) hardware generation teardown completed")
                slot.coordinator.mark_degraded()
                if slot.retarget_requested:
                    # An intentional retarget gets one immediate open
                    # attempt. If it fails, the ordinary stable-identity
                    # presence probe/backoff path owns subsequent retries.
                    slot.retarget_requested = False
                    self._output_dispatch_rebuild(slot)
        elif new_state == SlotState.DEGRADED.value:
            if slot.pending_bin is not None:
                print(f"  Output ({slot.name}) rebuild attempt failed health verification; will retry after backoff")
                self._output_discard_pending_bin(slot, abandoned=False)
            elif snapshot.get("operation_succeeded") is False:
                print(f"  Output ({slot.name}) quiesce operation failed unexpectedly; "
                      f"old generation reference abandoned, continuing")
        elif new_state == SlotState.RESTART_REQUIRED.value:
            if slot.pending_bin is not None:
                self._output_discard_pending_bin(slot, abandoned=True)
            print(f"  Output slot RESTART_REQUIRED ({slot.name}) -- a background hardware-state "
                  f"operation was abandoned; no further automatic rebuild attempts for this process lifetime")
            emit_event(
                category="hardware", level="error", title=f"{slot.name} output recovery abandoned",
                detail={
                    "generation": snapshot["generation"],
                    "loss_episode": slot.loss_episode,
                    "note": "A background hardware-state operation did not complete within its "
                            "timeout and was abandoned. Automatic retry has stopped for this slot; "
                            "an engine restart is the only way to fully reclaim it.",
                },
                # [P0] 1.3C physical-acceptance-failure fix -- stable
                # per-slot key; see the "output lost" key's comment.
                dedupe_key=f"hardware|output-restart-required|{slot.kind}",
            )

    @_glib_safe(default_return=True)
    def _output_recovery_tick(self):
        """GLib timer, ~300ms -- non-blocking. One tick services BOTH
        output slots; each keeps its own independent SlotCoordinator/
        backoff state. See _mic_recovery_tick's identical docstring for
        why this never touches GStreamer/Django state from inside a
        background worker thread, only from here on the GLib thread.

        [P0] 1.3C physical-acceptance-failure fix, round 1 -- the post-
        handler bookkeeping no longer writes the PRE-handler-captured
        `new_state` blindly. _output_handle_slot_transition can itself
        dispatch a FOLLOW-UP operation (e.g. the DEGRADED branch
        discarding a failed pending_bin, or the OK branch re-degrading
        after a bare quiesce) -- and _output_request_recovery already
        records THAT newer reality into slot.last_observed_slot_state
        synchronously, the moment its own request_recovery() call
        reports "dispatched" (see that method's docstring).

        [P0] 1.3C physical-acceptance-failure fix, round 2 (this
        review's own finding) -- round 1's fix, "re-read
        slot.coordinator.state fresh after the handler returns instead
        of reusing the stale pre-handler snapshot", was ITSELF still
        racy: if the nested operation dispatched from inside the
        handler resolves all the way back to OK before the handler call
        even returns (confirmed possible on real hardware-teardown
        timescales by physical UCA222 testing -- NULL workers can
        finish in milliseconds), then "re-read fresh" sees OK and
        erases the RECOVERING marker _output_request_recovery just
        installed, even though that marker was correct and no tick has
        observed it yet. A value comparison against coordinator.state
        (or against `old_state`, which round 1's docstring already
        rejected for a related reason) cannot tell "the handler
        dispatched nothing new" apart from "the handler dispatched
        something new that ALREADY finished" -- both present as "the
        state moved on" from the same starting point.

        Fixed with slot.recovery_dispatch_serial: bumped ONLY by
        _output_request_recovery on an actual NEW "dispatched" result,
        synchronously, same thread, same call. Captured here BEFORE
        calling the handler; compared AFTER it returns. If the serial
        is unchanged, nothing new was dispatched during handling --
        safe to synchronize last_observed_slot_state with the actual
        current coordinator state (round 1's behavior, still correct
        for that case). If the serial advanced, a nested dispatch
        happened -- last_observed_slot_state must be left exactly as
        _output_request_recovery set it (RECOVERING), REGARDLESS of
        whether that nested worker has already resolved further, so the
        NEXT tick is guaranteed to observe the RECOVERING -> (whatever
        it resolves to) transition and actually process the completion,
        rather than silently losing it the way the original gen0->
        gen563 storm depended on."""
        if not self.running:
            return True
        for slot in self._output_slots.values():
            slot.coordinator.tick()
            snapshot = slot.coordinator.snapshot()
            new_state = snapshot["state"]
            old_state = slot.last_observed_slot_state
            if new_state != old_state:
                dispatch_serial_before_handler = slot.recovery_dispatch_serial
                self._output_handle_slot_transition(slot, old_state, new_state, snapshot)
                if slot.recovery_dispatch_serial == dispatch_serial_before_handler:
                    # The handler did not dispatch another operation --
                    # safe to synchronize observation with the actual
                    # current state.
                    slot.last_observed_slot_state = slot.coordinator.state.value
                # else: the handler dispatched a new operation --
                # _output_request_recovery already installed the correct
                # RECOVERING marker; do not overwrite it even if that
                # worker has already resolved. The next tick must
                # observe RECOVERING -> (its actual outcome) and process
                # the completion.
        return True

    @_glib_safe(default_return=True)
    def _output_presence_probe_tick(self):
        """GLib timer, ~2s -- non-blocking (a plain /proc/asound/cards
        read per tick, shared across both slots -- see
        _mic_presence_probe_tick's identical docstring for why this is
        the deliberately-chosen fallback-probe cadence). Only acts on a
        slot that's DEGRADED with a stable identity configured and no
        operation in flight; blank identity means containment still
        applies but automatic rebuild-on-return never fires -- a raw
        numeric plughw:N,M path is not safe to retry against after a
        replug may have re-enumerated it at a different index."""
        if not self.running or not self._output_slots:
            return True
        cards = audio_recovery.read_alsa_cards_present()
        for slot in self._output_slots.values():
            if slot.identity_kind != "alsa_card_id" or not slot.identity:
                continue
            if slot.coordinator.state != audio_recovery.SlotState.DEGRADED:
                continue
            present = audio_recovery.alsa_card_identity_present(slot.identity, cards)
            slot.device_present = present
            if not present:
                continue
            now = time.monotonic()
            if slot.next_retry_at is not None and now < slot.next_retry_at:
                continue
            slot.recovery_attempt += 1
            slot.next_retry_at = now + audio_recovery.compute_backoff_seconds(slot.recovery_attempt)
            self._output_dispatch_rebuild(slot)
        return True

    def _output_recovery_state(self):
        """[P0] 1.3C -- engine_state.json's output_recovery block, keyed
        by slot kind ("studio_monitor"/"stereotool"). {} if no output
        slots exist at all (shouldn't happen in practice -- Studio
        Monitor's slot is always built -- but matches mic_recovery's own
        defensive shape for "nothing to report").

        [P0] 1.3C integration-bug fix -- added `active_device`,
        distinct from `resolved_runtime_device`. The latter is a PURE
        function of this slot's currently-known config (identity_kind/
        identity/legacy_device) -- what device SHOULD be in use given
        that config, not necessarily what the live sink actually has
        open right now. Production surfaced exactly this gap: a bug
        elsewhere briefly forced the live sink off its stable identity
        path while `resolved_runtime_device` kept reporting the stable
        path throughout, since it was never reading the sink itself.
        `active_device` reads current_sink.get_property("device")
        directly -- the actual live value, however it got there --
        so state reporting can never again be mistaken for proof of
        what the sink is really doing. `resolved_runtime_device` is
        left as-is (not renamed/removed) since it's still the right
        answer to "what does config say this should be"."""
        if not self._output_slots:
            return {}
        out = {}
        for slot in self._output_slots.values():
            snapshot = slot.coordinator.snapshot()
            next_retry_in_s = None
            if slot.next_retry_at is not None:
                next_retry_in_s = max(0.0, round(slot.next_retry_at - time.monotonic(), 1))
            try:
                active_device = slot.current_sink.get_property("device") if slot.current_sink else None
            except Exception:
                active_device = None
            out[slot.kind] = {
                "name": slot.name,
                "state": snapshot["state"],
                "generation": snapshot["generation"],
                "operation_state": snapshot["operation_state"],
                "device_present": slot.device_present,
                "configured_device": slot.legacy_device,
                "resolved_runtime_device": audio_recovery.resolve_runtime_device(
                    slot.identity_kind, slot.identity, slot.legacy_device),
                "active_device": active_device,
                "identity_kind": slot.identity_kind,
                "identity": slot.identity,
                "recovery_attempt": slot.recovery_attempt,
                "next_retry_in_s": next_retry_in_s,
                "last_error": slot.last_error,
                "last_state_change": slot.last_state_change_at,
                "restart_required": snapshot["state"] == audio_recovery.SlotState.RESTART_REQUIRED.value,
            }
        return out

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
        # [P0] 1.3C: the studio monitor alsasink is no longer built as a
        # static element here -- it's now the disposable hardware
        # generation inside self._studio_monitor_slot, built further down
        # (after agc_dynamic/agc_makeup/agc_limiter exist, since the
        # slot's queue links to agc_dynamic) via _build_output_slot.

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

        # [P0] 1.3C -- Studio Monitor's containment/recovery boundary.
        # Built UNCONDITIONALLY (unlike StereoTool below) -- per the task
        # design, Studio Monitor must have its own recovery boundary
        # regardless of whether StereoTool is configured at all; recovery
        # must never depend on having two outputs wired up. The stateful AGC
        # tail now lives inside the disposable generation so a branch-local
        # retarget also resets it after a silent-but-PLAYING failure.
        studio_monitor_device = self._resolve_studio_monitor_device()
        self._studio_monitor_slot = self._build_output_slot(
            STUDIO_MONITOR_NAME, "studio_monitor", studio_monitor_device,
            self._build_studio_monitor_hw_generation)

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
            self._studio_monitor_slot.queue, self._studio_monitor_slot.errorignore,
            self._studio_monitor_slot.valve, self._studio_monitor_slot.current_bin,
        ]

        # StereoTool bridge — raw (pre-AGC) tap off the mixer, split via a
        # tee right after format normalization. Only built at all if a
        # device is actually configured; otherwise the topology is
        # unchanged from before (capsfilter links directly to
        # studio_monitor_slot.queue -- see the linking section below).
        #
        # [P0] 1.3C: this branch's queue/sink special properties (leaky
        # policy, sync/async, buffer-time/latency-time) are unchanged
        # from before this phase -- see _build_output_containment_queue
        # and _build_stereotool_hw_generation for exactly the same
        # settings/reasoning, now reapplied on EVERY generation rebuild
        # (not just this first one) since the alsasink itself is now
        # disposable.
        stereotool_device = self._resolve_stereotool_device()
        stereotool_tee = None
        self._stereotool_slot = None
        if stereotool_device:
            stereotool_tee = Gst.ElementFactory.make("tee", "stereotool_tee")
            self._stereotool_slot = self._build_output_slot(
                STEREOTOOL_OUTPUT_NAME, "stereotool", stereotool_device,
                self._build_stereotool_hw_generation)
            elements += [
                stereotool_tee,
                self._stereotool_slot.queue, self._stereotool_slot.errorignore,
                self._stereotool_slot.valve, self._stereotool_slot.current_bin,
            ]

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
        # (unconditional -- output_level is always in the pipeline) and
        # the mic/output error router can subscribe.
        bus = self.main_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::element", self._on_element_message)
        # [P0] 1.3C: connected UNCONDITIONALLY now -- was previously
        # gated on `if mic_elements:`, a real limitation this phase
        # fixes (see _on_main_bus_error's own docstring). Output
        # recovery needs this router regardless of whether a studio mic
        # is configured at all, and the Studio Monitor slot always
        # exists.
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
            stereotool_tee.link(self._studio_monitor_slot.queue)
            stereotool_tee.link(self._stereotool_slot.queue)
        else:
            self.output_level.link(self._studio_monitor_slot.queue)

        # Each branch keeps its own persistent containment queue,
        # errorignore, and valve. Studio Monitor's disposable generation
        # begins after that valve and includes its processing tail + sink;
        # StereoTool's generation remains sink-only.
        self._studio_monitor_slot.queue.link(self._studio_monitor_slot.errorignore)
        self._studio_monitor_slot.errorignore.link(self._studio_monitor_slot.valve)
        self._studio_monitor_slot.valve.get_static_pad("src").link(
            self._studio_monitor_slot.current_bin.get_static_pad("sink"))

        if self._stereotool_slot:
            self._stereotool_slot.queue.link(self._stereotool_slot.errorignore)
            self._stereotool_slot.errorignore.link(self._stereotool_slot.valve)
            self._stereotool_slot.valve.get_static_pad("src").link(
                self._stereotool_slot.current_bin.get_static_pad("sink"))

        self._output_slots = {
            slot.kind: slot for slot in (self._studio_monitor_slot, self._stereotool_slot) if slot is not None
        }

        self._apply_agc_config()

        self.main_pipeline.set_state(Gst.State.PLAYING)

    def _apply_agc_config(self):
        """Apply live AGC settings and cache them for future generations.

        The properties are controllable and safe to update while PLAYING.
        Current and pending Studio Monitor generations receive the setting;
        a generation built after this call reads the same cached values.
        """
        close_old_connections()
        cfg = AudioOutput.objects.filter(name=STUDIO_MONITOR_NAME).first()
        enabled = bool(cfg and cfg.agc_enabled)
        self._studio_monitor_agc_settings = {
            "enabled": enabled,
            "ratio": cfg.agc_ratio if cfg else 1.0,
            "threshold": cfg.agc_threshold if cfg else 0.0,
            "soft_knee": cfg.agc_soft_knee if cfg else False,
            "makeup_gain_db": cfg.agc_makeup_gain_db if cfg else 0.0,
        }
        slot = self._studio_monitor_slot
        for bin_ in (slot.current_bin, slot.pending_bin):
            if bin_ is not None:
                self._configure_studio_monitor_generation(
                    bin_, self._studio_monitor_agc_settings)
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

    def _active_log_has_committed_playout(self):
        """Whether the active log still owns real program audio to play.

        This is deliberately cursor-aware. A cursor at ``len(log_items)``
        does not mean the log is exhausted if its last item is already on a
        deck; conversely, an old PlaylistLog row by itself is not runway.
        Only an unpaused, unfinished deck from the active log or a playable
        future item at/after the cursor counts.
        """
        if self.current_log is None:
            return False
        active_log_id = self.current_log.id
        with self._lock:
            decks = tuple(self.decks.values())
            cursor = self._queue_cursor
            items = tuple(self.log_items[cursor:])

        for deck in decks:
            if deck is None or getattr(deck, "paused", False) or getattr(deck, "finished", False):
                continue
            log_item = getattr(deck, "log_item", None)
            if (
                log_item is not None
                and log_item.playlist_log_id == active_log_id
                and getattr(deck, "track", None) is not None
            ):
                return True

        return any(
            item.playlist_log_id == active_log_id and _log_item_playable(item)[0]
            for item in items
        )

    def _current_hour_schedule_state(self, now):
        """Central current-hour orchestration classification.

        ``resolve_schedule_block`` intentionally remains exact-start. A
        blank wall-clock hour is a healthy continuation only while an older
        active log still has committed playout; an exhausted/missing old log
        is an unscheduled gap, not silently accepted merely because a stale
        PlaylistLog row exists.
        """
        now_key = (now.date(), now.hour)
        active_key = (
            (self.current_log.date, self.current_log.hour)
            if self.current_log is not None else None
        )
        schedule_block = resolve_schedule_block(now.date(), now.hour)

        if schedule_block is not None:
            state = "scheduled"
            has_committed_playout = False
        elif active_key is not None and active_key > now_key:
            state = "early_rollover"
            has_committed_playout = False
        elif active_key == now_key:
            state = "current_log"
            has_committed_playout = self._active_log_has_committed_playout()
        else:
            has_committed_playout = self._active_log_has_committed_playout()
            state = (
                "continuation"
                if active_key is not None and active_key < now_key and has_committed_playout
                else "unscheduled_gap"
            )

        return {
            "state": state,
            "now_key": now_key,
            "active_key": active_key,
            "schedule_block": schedule_block,
            "schedule_expected": schedule_block is not None,
            "has_committed_playout": has_committed_playout,
        }

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

        hour_state = self._current_hour_schedule_state(now)

        # Kick off (or no-op if already approved/in-flight/unscheduled) BEFORE the
        # monitoring check below, so a first-tick "no log yet" event
        # accurately reports build_in_progress=True rather than False
        # for the instant before the NEXT tick would otherwise have
        # been the first to notice the worker it just started.
        self._ensure_log_building(
            now.date(), now.hour,
            schedule_expected=hour_state["schedule_expected"],
        )

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
        now_key = hour_state["now_key"]
        active_key = hour_state["active_key"]

        if hour_state["state"] == "scheduled" and active_key is not None and active_key < now_key:
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
        elif hour_state["state"] == "scheduled" and active_key is None:
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
        elif hour_state["state"] == "unscheduled_gap":
            emit_event(
                category="engine", level="warning",
                title="Unscheduled hour has no continuing program",
                detail={
                    "target_date": str(now.date()), "target_hour": now.hour,
                    "active_log_date": str(self.current_log.date) if self.current_log else None,
                    "active_log_hour": self.current_log.hour if self.current_log else None,
                    "has_committed_playout": False,
                },
                dedupe_key=f"engine|unscheduled-hour-gap|{now.date()}|{now.hour}",
            )
        # continuation/current_log/early_rollover are all intentional,
        # warning-free states. In particular, an older active log is only
        # accepted here when _active_log_has_committed_playout() proved it.

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
            if active_key is not None and active_key > now_key:
                # Intentional early rollover: never move the queue backward.
                pass
            elif active_key == now_key or hour_state["schedule_expected"]:
                self._load_log_for(now.date(), now.hour)
            # During a blank continuation/gap, retain the older active log.
            # Its queued items or an async live-fill proposal still belong to
            # that log; loading the nonexistent blank hour here would clear
            # current_log and make either recovery path stale immediately.
            if self.log_items and (
                hour_state["state"] != "unscheduled_gap"
                or self._queue_cursor < len(self.log_items)
            ):
                self._start_next_track()
            elif (
                hour_state["state"] == "unscheduled_gap"
                and active_key is not None and active_key < now_key
            ):
                self._try_extend_live_log_async()

        seconds_left_in_hour = 3600 - (now.minute * 60 + now.second)
        if seconds_left_in_hour <= NEXT_HOUR_LOOKAHEAD_SECONDS:
            next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            # Clock-drift recovery (1.1 spec): project the upcoming
            # hour's REAL takeover time from live queue/deck state
            # before kicking off its build, so a track already known to
            # be running past nominal_start doesn't get a target that
            # assumes a full, on-time hour. Only meaningful here, the
            # actual next-hour lookahead build -- NOT the current-hour
            # catch-up call above, which is a startup/recovery scenario.
            target_duration_seconds = self._project_upcoming_hour_target_duration(next_hour)
            self._ensure_log_building(next_hour.date(), next_hour.hour, target_duration_seconds=target_duration_seconds)
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

    def _ensure_log_building(
        self,
        target_date,
        target_hour,
        target_duration_seconds=NOMINAL_HOUR_SECONDS,
        schedule_expected=None,
    ):
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
        (12pm news repeated).)

        Missing exact-start ScheduleBlocks are deterministic and return
        ``"unscheduled"`` without spawning a worker. ``schedule_expected``
        lets _ensure_upcoming_logs reuse its centralized current-hour
        classification; omitted for other callers, this method performs the
        same exact-start resolution itself. This does not broaden schedule
        resolution or clone an earlier block into a blank hour.

        `target_duration_seconds` defaults to a full nominal hour --
        _ensure_upcoming_logs passes a shorter, clock-drift-projected
        value for the NEXT hour's lookahead build only; the current-
        hour catch-up build (a startup/recovery scenario, not "predict
        the future") always uses the default."""
        if PlaylistLog.objects.filter(date=target_date, hour=target_hour, status="approved").exists():
            return "approved"
        if schedule_expected is None:
            schedule_expected = resolve_schedule_block(target_date, target_hour) is not None
        if not schedule_expected:
            return "unscheduled"
        key = (target_date, target_hour)
        with self._lock:
            if key in self._building_hours:
                return "building"
            self._building_hours.add(key)
        threading.Thread(
            target=self._build_hour_log_worker, args=(target_date, target_hour, target_duration_seconds),
            daemon=True,
        ).start()
        return "dispatched"

    def _build_hour_log_worker(self, target_date, target_hour, target_duration_seconds=NOMINAL_HOUR_SECONDS):
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
            log, error = build_and_approve_hour_log_locked(target_date, target_hour, target_duration_seconds=target_duration_seconds)
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

    def _committed_future_runway_seconds(self):
        """Authoritative "how much airtime is already committed to play
        between NOW and whatever finishes last in the current queue" --
        used by _try_extend_live_log_async to decide whether a live-fill
        dispatch is even warranted.

        Split cleanly into two independent parts, mirroring
        _compute_queue_eta_state's own listener-audible-start semantics
        (see that function's docstring for the full derivation of why
        the first offset is fundamentally different from every one
        after it):

          1. Remaining playout on the LEADING deck (the soonest-to-
             finish, unpaused, occupied deck): its own next_start
             minus its CURRENT source position. Its cue_in already
             happened, so must NOT be re-subtracted. Delegates to
             `_leading_deck_eta_seconds` -- the same function that
             seeds the operator-facing "Starts at" queue ETA, so
             these two numbers can never silently drift apart.

          2. FUTURE queued items only: everything at
             self.log_items[self._queue_cursor:], accumulated via
             log_builder's own effective_airtime_seconds() (imported
             at module top). Explicitly does NOT sum the entire
             log_items list -- an already-played item (index below
             the cursor) has already contributed its airtime to the
             hour and must never be counted again toward remaining
             runway. Same reason a substitute/skipped item (which
             _next_queue_item advances past) is naturally excluded:
             the cursor has already moved beyond it, so it never
             enters this sum.

        Deliberately does NOT include forced items
        (self._forced_next_items) even though _compute_queue_eta_state's
        preview does. Forced items are spliced IN FRONT of the normal
        queue at play time, replacing what the cursor would otherwise
        return; counting both would double-book the same time. In the
        common case forced items either aren't present or are dedication
        intros that displace far more time than they add (a 4-second
        intro pushed in front of a 3-minute song is trivial compared to
        the honest deficit calculation this method is used for). If a
        future forced-item feature ever pushes multi-minute content,
        this can be revisited -- for the false-deficit-suppression
        purpose here, ignoring them is safe (biases slightly toward
        under-estimating committed runway, i.e. toward LETTING a fill
        happen rather than suppressing one that shouldn't).

        Returns 0.0 if there's nothing playing and nothing queued -- a
        genuine "queue empty" state that a live-fill IS legitimately
        for."""
        runway = self._leading_deck_eta_seconds()
        cursor = self._queue_cursor
        for item in self.log_items[cursor:]:
            track = item.track
            if track is None:
                # Deleted-track ghost -- _next_queue_item will skip it,
                # so it contributes zero real airtime. Same reason
                # _get_upcoming_preview filters these out of the UI queue.
                continue
            runway += effective_airtime_seconds(track)
        return runway

    def _try_extend_live_log_async(self):
        """Throttled DISPATCH of an async live-log-fill worker -- called
        both proactively (from `_poll_position`'s crossfade lookahead,
        every ~250ms, once the current hour's queue is exhausted with no
        real next hour ready yet) and reactively (from
        `_roll_over_to_next_hour`, at the moment the last known track
        hits natural EOS with nothing queued to follow it). 1.1 spec:
        moved off the GLib thread -- the actual DB work used to run
        synchronously, inline, on this thread. The worker
        (`_live_fill_worker`) is READ-ONLY -- it computes a PROPOSAL
        only and never touches PlaylistLog/LogItem; the DB write and
        the in-memory queue extension both happen later, only after
        `_install_live_fill` re-validates the proposal is still current
        (see that function's docstring for the full staleness-check
        list). A stale worker result must be completely side-effect-
        free -- this is what makes that guarantee hold even under a
        rollover/newer-dispatch/shutdown race.

        Always returns False -- unlike the historical synchronous
        version, this never has a result to report the SAME tick it's
        called; a valid fill lands a little later via
        `_install_live_fill`, which (mirroring `_install_built_hour`'s
        own idle-recovery check) starts playback immediately if a slot
        is sitting idle waiting for it.

        Throttled to once per 5s (`self._last_live_extend_attempt`)
        since a persistently-empty fallback category (e.g. `LogFill
        Config` misconfigured) would otherwise dispatch a new worker on
        every single poll tick for however long the current track has
        left to play. Also guarded by `self._live_fill_in_progress` so
        the reactive and proactive call sites can't both dispatch at
        once. `self._live_fill_generation` is bumped on every dispatch
        (even a throttled-away one doesn't bump it, only a REAL
        dispatch does) so `_install_live_fill` can detect and discard a
        proposal superseded by a newer dispatch, even though only one
        worker is ever in flight at a time -- the guard is cleared by
        the WORKER's own completion, not by the (possibly delayed)
        GLib idle callback, so a second dispatch can start before the
        first one's install callback has actually run.

        False-deficit suppression (2026-08-12 fix): the old version
        conflated "the cursor reached the end of self.log_items" with
        "no runway remains before TOH" -- true when the last-queued
        item is short, dramatically false when it's a long mixshow
        segment (a real live production case appended ~26 minutes of
        PSAs while a ~26.5-minute Grateful Dead Hour Part 2 was still
        playing and about to naturally carry the hour to TOH). The
        cursor reaches the end of log_items the instant the LAST
        item is dequeued for playback, not when that item finishes,
        so a proactive `_try_extend_live_log_async` call from
        `_poll_position` fires almost the entire item's duration
        before the actual end. Fix: compute an authoritative
        committed_runway (see _committed_future_runway_seconds --
        remaining playout of the leading deck + effective airtime of
        every FUTURE queued item, cursor-aware), compare to
        wall-clock-remaining-in-hour, and dispatch only when the
        deficit is genuinely positive beyond DURATION_FIT_MARGIN. TOH
        itself is the authoritative target ceiling here regardless of
        the built hour's clock-drift-adjusted target -- rollover at
        _advance_to_next_hour_log's ~30s-before-TOH mark discards
        whatever's left unplayed anyway, so scheduling PAST TOH is
        pointless (and passing target_duration_seconds=wall_remaining
        into the worker below makes fill_remaining_hour target that
        real remaining window, not a hardcoded 3600, closing the
        secondary "flat 3600" issue as a side effect of the same
        computation)."""
        now = time.time()
        if now - self._last_live_extend_attempt < 5.0:
            return False
        self._last_live_extend_attempt = now

        with self._lock:
            if self._live_fill_in_progress:
                return False
            if not self.current_log or not self.log_items:
                return False
            self._live_fill_in_progress = True
            self._live_fill_generation += 1
            generation = self._live_fill_generation

        wall_now = timezone.localtime()
        seconds_left = 3600 - (wall_now.minute * 60 + wall_now.second)
        if seconds_left <= DURATION_FIT_MARGIN:
            # Basically at the real boundary anyway -- clear the guard
            # we just took, since no worker is being dispatched.
            with self._lock:
                self._live_fill_in_progress = False
            return False

        # Authoritative committed-runway check (see this function's
        # own docstring for the full "false-deficit suppression"
        # rationale). Even if the cursor has "run out" of log_items,
        # the LEADING deck may still have most of a long track left
        # to play, which counts. Skip dispatch entirely when the
        # deficit is not genuinely positive beyond the standard fit
        # margin -- do NOT dispatch a worker just to have it compute
        # a proposal that would then be discarded downstream, since
        # a proposal that actually adds nothing would go
        # unrepresented and a proposal that adds "just a little" is
        # exactly the false-fill case this suppresses.
        committed_runway = self._committed_future_runway_seconds()
        deficit = seconds_left - committed_runway
        if deficit <= DURATION_FIT_MARGIN:
            with self._lock:
                self._live_fill_in_progress = False
            return False

        # Capture an IMMUTABLE snapshot of everything the worker needs --
        # never hand the worker thread self.current_log/self.log_items
        # directly, since a rollover on this (the main) thread could
        # replace either while the worker is still running.
        log_id = self.current_log.id
        hour_start = wall_now.replace(minute=0, second=0, microsecond=0)
        existing_picks = [{"track": li.track, "category": li.category} for li in self.log_items]
        original_count = len(existing_picks)
        # accumulated + target_duration_seconds together tell
        # fill_remaining_hour "there's `deficit` seconds left to fill"
        # -- expressed in its own (target - accumulated) contract. The
        # exact absolute values don't matter as long as their
        # difference equals the real deficit, so target=deficit +
        # DURATION_FIT_MARGIN, accumulated=0 keeps the math trivial
        # and self-documenting. (The old bug was accumulated = 3600 -
        # seconds_left with target defaulted to NOMINAL_HOUR_SECONDS,
        # which computed `remaining = seconds_left` -- WALL clock, not
        # deficit, so a big-committed-but-empty-queue state got
        # tricked into over-filling.)
        accumulated = 0.0
        target_duration_seconds = deficit
        start_position = self.log_items[-1].position + 1

        threading.Thread(
            target=self._live_fill_worker,
            args=(log_id, generation, existing_picks, original_count, accumulated, hour_start, start_position, target_duration_seconds),
            daemon=True,
        ).start()
        return False

    def _live_fill_worker(self, log_id, generation, existing_picks, original_count, accumulated, hour_start, start_position, target_duration_seconds=NOMINAL_HOUR_SECONDS):
        """Runs on a background thread -- READ-ONLY with respect to the
        database. Computes a PROPOSED set of fill picks via
        fill_remaining_hour (which issues real SELECT queries, e.g.
        pick_track's candidate pools, but never writes) and hands the
        proposal back to the main thread via GLib.idle_add. Never calls
        append_fill_items or otherwise inserts/updates/deletes
        PlaylistLog/LogItem rows itself, and never touches
        self.log_items/self.current_log/self._queue_cursor or any
        GStreamer object -- the DB write and the in-memory extension
        both happen only in _install_live_fill, and only after that
        callback re-validates the proposal is still current on the
        main thread. A stale worker (its result superseded by a
        rollover, a newer dispatch, the gap already being filled by
        something else, or plain shutdown) is therefore automatically
        side-effect-free -- it simply computed something nobody used.

        `target_duration_seconds` defaults to NOMINAL_HOUR_SECONDS for
        backward compatibility with any older test that constructs a
        worker call directly, but the real dispatcher
        (`_try_extend_live_log_async`) always passes the authoritative
        deficit computed from current engine state (see that function's
        docstring for the "false-deficit suppression" derivation) --
        NOT a hardcoded 3600. This is what makes fill_remaining_hour
        target the real remaining window rather than assuming a full
        hour is empty."""
        try:
            close_old_connections()
            # fill_remaining_hour appends onto `existing_picks` in place
            # and returns that same list, so the new items must be
            # sliced off using the count captured *before* the call.
            all_picks, _ = fill_remaining_hour(
                existing_picks, accumulated, hour_start,
                target_duration_seconds=target_duration_seconds,
            )
            new_picks = all_picks[original_count:]
            if not new_picks:
                return
            if self.running:  # don't schedule queue mutations mid-teardown
                GLib.idle_add(self._install_live_fill, log_id, generation, new_picks, original_count, start_position)
        except Exception as exc:
            print(f"  Live-fill worker crashed (non-fatal): {exc}")
            emit_event(
                category="engine", level="error", title="Live-fill worker crashed",
                detail={"log_id": log_id, "exception": repr(exc), "traceback": traceback.format_exc()},
            )
        finally:
            try:
                connection.close()  # don't leak a per-thread DB connection over a long engine uptime
            finally:
                with self._lock:
                    self._live_fill_in_progress = False

    @_glib_safe(default_return=False)
    def _install_live_fill(self, log_id, generation, new_picks, expected_prior_count, start_position):
        """One-shot GLib.idle_add callback -- runs on the main thread,
        so it's safe to touch engine state here, and is the ONLY place
        a live-fill proposal is allowed to touch the database. A stale
        proposal is discarded with ZERO side effects -- no DB write, no
        self.log_items mutation -- before any of that happens. Checked,
        in order:

          1. self.running -- mid-teardown, no queue mutations at all.
          2. self.current_log still exists and its id matches log_id --
             an hour rollover (or a different admin/engine rebuild)
             while the worker ran means this proposal is for an hour
             that's no longer live.
          3. `generation` matches self._live_fill_generation -- a newer
             dispatch has already superseded this one (the guard is
             cleared by worker completion, not by this callback
             actually running, so a second dispatch CAN start before
             this one's callback fires).
          4. len(self.log_items) still equals the count captured at
             dispatch time -- something else (a different install, a
             manual admin action) already changed the queue since this
             proposal was computed; appending on top of an assumption
             that's no longer true risks either duplicating coverage or
             mis-positioning items.
          5. Real wall-clock "seconds left in the real hour" is
             recomputed fresh (not reused from dispatch time) and must
             still be above DURATION_FIT_MARGIN -- a slow worker or a
             delayed idle-callback scheduling could have carried us
             past the real hour boundary (or close enough that the gap
             this proposal was computed for no longer exists) even if
             current_log/log_items look unchanged.

        Only once every check passes does the DB append
        (append_fill_items, transactional) happen, and only after that
        succeeds does self.log_items get extended -- DB before memory,
        never the reverse, and never at all for a stale proposal.

        Mirrors _install_built_hour's own idle-recovery check: a slot
        sitting empty specifically because it was waiting on this fill
        must resume immediately, not wait for the next _poll_position
        tick or (worse) _on_log_exhausted's 30s retry timer."""
        if not self.running:
            return False
        if not self.current_log or self.current_log.id != log_id:
            print(f"  Discarding stale live-fill proposal for log id={log_id} -- active log has since changed")
            return False
        if generation != self._live_fill_generation:
            print(f"  Discarding stale live-fill proposal for log id={log_id} -- superseded by a newer live-fill")
            return False
        if len(self.log_items) != expected_prior_count:
            print(f"  Discarding stale live-fill proposal for log id={log_id} -- queue already changed since dispatch")
            return False

        wall_now = timezone.localtime()
        seconds_left_now = 3600 - (wall_now.minute * 60 + wall_now.second)
        if seconds_left_now <= DURATION_FIT_MARGIN:
            print(f"  Discarding stale live-fill proposal for log id={log_id} -- real hour boundary reached since dispatch")
            return False

        close_old_connections()
        try:
            with transaction.atomic():
                log = PlaylistLog.objects.get(id=log_id)
                new_items = append_fill_items(log, new_picks, start_position)
        except PlaylistLog.DoesNotExist:
            print(f"  Discarding live-fill proposal for log id={log_id} -- PlaylistLog no longer exists")
            return False
        except Exception as exc:
            print(f"  Live-fill DB append failed (non-fatal): {exc}")
            emit_event(
                category="engine", level="error", title="Live-fill DB append failed",
                detail={"log_id": log_id, "exception": repr(exc), "traceback": traceback.format_exc()},
            )
            return False

        # DB succeeded -- only NOW extend the in-memory queue.
        self.log_items.extend(new_items)
        print(f"  Installed {len(new_items)} live-filled track(s) onto the active queue "
              f"({seconds_left_now:.0f}s left in the real hour)")
        # Informational, lower severity than the "log not ready" warnings
        # in _ensure_upcoming_logs -- this path fires for two different
        # reasons (ordinary early exhaustion mid-hour, e.g. a DJ skipped
        # ahead of schedule; or the real next hour's async build running
        # long/failing) and this event alone doesn't distinguish them,
        # it's just visibility that the fallback engaged at all.
        emit_event(
            category="engine", level="info", title="Live log extension activated",
            detail={
                "log_id": log_id, "date": str(log.date), "hour": log.hour,
                "items_added": len(new_items), "seconds_left_in_hour": round(seconds_left_now, 1),
            },
        )

        with self._lock:
            idle = self.decks["A"] is None and self.decks["B"] is None
        if idle and not self.manual_mode:
            self._start_next_track()
        return False

    def _roll_over_to_next_hour(self):
        """Called when the current hour's queue is exhausted. If the next
        hour's log has already been auto-built (normally true — it's
        approved NEXT_HOUR_LOOKAHEAD_SECONDS before top of hour), switch
        the engine's active log over to it so playback and the crossfade
        trigger both continue seamlessly across the boundary, instead of
        waiting for the natural-EOS `_on_log_exhausted` path to notice.
        If the real next hour isn't built yet — early exhaustion, before
        the actual top of hour — dispatch an async live-fill instead of
        returning False outright (which would fall through to
        `_on_log_exhausted`'s replay-current-hour-from-scratch
        fallback). The dispatch itself never completes synchronously
        (see `_try_extend_live_log_async`), so this still returns
        False on that path -- `_on_log_exhausted`'s own recovery
        (immediate next-hour check, then a 30s retry poll) covers the
        gap until `_install_live_fill` lands, which in the common case
        (proactive dispatch already well underway from `_poll_position`
        before actual exhaustion) has already happened by the time
        this reactive path is even reached."""
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
        self._try_extend_live_log_async()
        return False

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
                seek_ok = deck.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    _auto_resume_position_ns,
                )
                if not seek_ok:
                    # Confirmed via isolated repro of the 2026-08-01
                    # incident that this does NOT catch the transient
                    # post-seek parser hiccup (seek_simple still
                    # returns True there) -- this path is for an
                    # outright-rejected seek instead (e.g. wrong
                    # pipeline state), a different, rarer failure mode
                    # worth its own visibility.
                    print(f"  [{slot}] Auto-resume seek to {_auto_resume_position_ns / Gst.SECOND:.1f}s rejected")
                    emit_event(
                        category="engine", level="error", title="Deck seek rejected",
                        detail={"slot": slot, "track_id": track.id,
                                "target_seconds": _auto_resume_position_ns / Gst.SECOND},
                        dedupe_key=f"engine|seek-rejected|slot={slot}|track={track.id}",
                    )
                else:
                    deck.seeked_at = time.time()
                    # started_at drives _get_deck_position; adjust it
                    # so UI position readouts line up with the audio.
                    # Only when the seek actually succeeded -- a
                    # rejected seek leaves the deck genuinely at 0,
                    # and started_at was already set correctly for
                    # that by the code above.
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

    def _fx_fires_state(self):
        """Serializable snapshot of currently-active FX fires, for the
        dashboard's authoritative-state reconciliation (see
        dashboard.html's reconcileFxFires/_fxSyncButton). Read by
        _write_state and exposed verbatim through /api/engine/status/,
        same pass-through pattern that file already uses for deck/
        queue state.

        Each entry is {"cart_id": int, "elapsed_seconds": float}.
        VT fires (cart_id is None -- see _vt_fire_file) are excluded:
        they're not cart-button-driven and have no dashboard element to
        match against.

        elapsed_seconds is computed HERE, server-side, at write time --
        the same "elapsed-at-write-time" convention _write_state already
        uses for deck position (self._get_deck_position, rounded and
        embedded directly rather than a raw start timestamp). This
        deliberately avoids ever serializing the engine's own
        wall-clock started_at for the browser to diff against its own
        Date.now() -- a different process/clock domain. The browser
        only ever needs "how far into playback is this, as of the
        state snapshot whose overall staleness state["timestamp"]
        already governs" -- not an absolute instant to reconcile
        against its own clock.

        Deliberately does NOT include duration_seconds: each dashboard
        button already carries its own FXCart.duration_seconds via
        data-duration (rendered server-side at page load, from the
        exact same field) -- serializing it a second time here would
        just be a second, no-fresher copy of the same value to keep in
        sync for no benefit, since a stale page reload already picks up
        the current duration on its own.

        Multiple simultaneous fires of the SAME cart_id are returned as
        separate list entries, not collapsed -- _fx_fire's own
        retrigger handling (see its 'ignore'/'stop'/'restart' branches)
        happens to make >1 concurrent fire per cart_id impossible today
        for any retrigger mode, but this function doesn't assume that;
        it's the caller (dashboard JS, which has exactly one button per
        cart) that reduces this down, not the wire format."""
        with self._fx_lock:
            fires = list(self._fx_fires.values())
        now = time.time()
        out = []
        for f in fires:
            cart_id = f.get("cart_id")
            if cart_id is None:
                continue
            elapsed = max(0.0, now - f["started_at"])
            out.append({"cart_id": cart_id, "elapsed_seconds": round(elapsed, 1)})
        return out

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

        # Branch-local completion signal -- deliberately NOT the shared
        # pipeline bus. See _fx_install_completion_probe's own
        # docstring for why message::eos never reaches the bus for a
        # branch feeding fx_submix, verified empirically rather than
        # assumed.
        self._fx_install_completion_probe(gain.get_static_pad("src"), fire_id)

        # State: PLAYING. Same order as _create_deck's sync_state pattern.
        for el in (filesrc, decodebin, convert, resample, fx_caps, gain):
            el.sync_state_with_parent()

        print(f"  FX fire {fire_id}: cart {cart_id} ({cart.name!r})")
        return True

    def _fx_install_completion_probe(self, pad, fire_id):
        """Installs a branch-local EOS probe on `pad` -- the last real
        element's src pad before it joins fx_submix (see _fx_fire's
        `gain` element / _vt_fire_file's own) -- that detects THIS
        branch's own natural completion, independent of the shared
        main pipeline ever reaching a pipeline-wide EOS.

        Why not the pipeline bus (this file used to watch
        "message::eos" there via _fx_on_eos, now removed): fx_submix
        is a live GstAggregator with other permanently-running inputs
        (including its own permanent silence branch -- see _fx_setup).
        A GstAggregator only posts a pipeline-level EOS once EVERY
        sink pad has seen its own EOS, which for a mixer with an
        always-live input can structurally never happen. This isn't
        theoretical -- it's exactly the live bug this method fixes: a
        fired cart's own state entry was confirmed (2026-08 live
        verification) to sit in self._fx_fires indefinitely, well past
        its real duration, because _fx_on_eos's bus watch simply never
        fired. _create_deck's own eos_probe (ghost_pad, feeding
        self.mixer) already solves the identical problem the identical
        way for decks -- this is the same idiom, applied to the FX/VT
        submix path, confirmed via an isolated offline harness
        matching this exact topology (live audiomixer + permanent
        silence input + one-shot filesrc branch) before touching this
        code: the branch-local pad probe reliably observes the EOS
        that never reaches the bus, and clean teardown immediately
        afterward (set NULL, unlink, release_request_pad, remove from
        pipeline) leaves the mixer/pipeline running normally.

        Drops the EOS event here (Gst.PadProbeReturn.DROP) rather than
        letting it continue into fx_submix's request pad -- same
        choice _create_deck's own eos_probe already makes, for the
        same reason, and reconfirmed safe here via that same offline
        harness.

        Streaming-thread safety: this probe callback runs on
        GStreamer's own streaming thread, NOT the GLib main-loop
        thread. It must never itself perform destructive teardown
        (set_state(NULL), pipeline.remove(), release_request_pad()) --
        it only schedules _fx_fire_completed via GLib.idle_add (
        documented safe to call from any thread), which does the
        actual teardown on the main loop, same handoff pattern
        _on_deck_eos_probed's own installation already uses."""
        def _completion_probe(pad, info, fire_id=fire_id):
            event = info.get_event()
            if event.type == Gst.EventType.EOS:
                GLib.idle_add(self._fx_fire_completed, fire_id)
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, _completion_probe)

    @_glib_safe(default_return=False)
    def _fx_fire_completed(self, fire_id):
        """Shared branch-completion path for an FX/VT fire reaching its
        own natural end -- scheduled via GLib.idle_add from
        _fx_install_completion_probe's pad probe, never called
        directly from a streaming thread. Replaces the old
        _fx_on_eos (bus-watch based, removed -- see
        _fx_install_completion_probe's docstring for why it never
        actually fired).

        Looks the fire up fresh rather than trusting the caller still
        has a live reference: a retrigger-triggered _fx_stop() (main-
        thread, synchronous, e.g. an operator's "restart" click) may
        already have torn this exact fire down by the time this idle
        callback runs. A missing fire_id is a silent, safe no-op --
        the same pop-or-None idempotency _fx_stop() itself already
        relies on, which is also what makes a duplicate/late-scheduled
        completion for the same fire_id harmless (whichever runs
        first wins; the second finds nothing to do).

        Ordering preserved exactly as the old _fx_on_eos had it: for a
        VT fire, advance the VT state machine BEFORE tearing down --
        _vt_handle_outgoing_ended needs to see the in-flight fire_id
        still present in self._fx_fires while it decides whether the
        outro-VT is still going."""
        with self._fx_lock:
            state = self._fx_fires.get(fire_id)
        if state is None:
            return False
        vt_kind = state.get("vt_kind")
        if vt_kind:
            self._vt_on_fire_eos(vt_kind, fire_id)
        self._fx_stop(fire_id)
        return False

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

        # Same branch-local completion signal _fx_fire uses -- see
        # _fx_install_completion_probe's docstring. VTs go through the
        # identical fx_submix-feeding topology, so they had the exact
        # same "message::eos never reaches the bus" bug.
        self._fx_install_completion_probe(gain.get_static_pad("src"), fire_id)
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
        """Called from _fx_fire_completed when a VT fire ends. Advances the
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
                # [P0] 1.3C: also refreshes every output slot's recovery-
                # identity fields as part of the same command -- see
                # _reload_output_recovery_identity's own docstring for
                # why this is folded in here rather than as a second,
                # separately-fired command (engine_cmd.json is a single-
                # slot channel; a second _write_engine_command call from
                # the same admin save would just overwrite this one
                # before the engine ever reads it).
                #
                # [P0] 1.3C integration-bug fix -- AGC reapply used to be
                # dispatched as a SEPARATE "reload_agc_config" command,
                # written directly by AudioOutputAdmin.save_model() after
                # super().save_model() had already let this signal fire.
                # Both writers targeted the same single-slot
                # engine_cmd.json, so the admin's later write reliably
                # clobbered this one before the engine ever polled it --
                # a Studio Monitor save's identity refresh was silently
                # lost every time, only the AGC reapply ever landed.
                # Fixed by making "reload_audio_output" Studio Monitor's
                # ONE unified live-reload command: identity refresh,
                # device swap, AND AGC reapply, all under this single
                # command. See hardware/admin.py's save_model for the
                # writer-side half of this fix.
                self._apply_agc_config()
                identity_changes = self._reload_output_recovery_identity()
                self._apply_audio_output_device(
                    self._resolve_studio_monitor_device(),
                    identity_changed="studio_monitor" in identity_changes)
            elif cmd == "reload_audio_output_recovery_config":
                # [P0] 1.3C -- for AudioOutput rows other than Studio
                # Monitor (Stereotool Input today), which have no live
                # device-swap path at all -- see hardware/signals.py.
                self._reload_output_recovery_identity()
            elif cmd == "reload_agc_config":
                # No longer written anywhere as of the integration-bug
                # fix above (AGC reapply now rides along with
                # "reload_audio_output" instead) -- kept as a harmless,
                # still-correct standalone handler in case some future
                # caller wants AGC-only reapply without the rest.
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
        live-log-mutation pattern _install_live_fill already uses
        safely (append to self.log_items on the main thread only), just
        inserting at the front of the queue instead of the back.

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
        # [4.1] Same read also seeds session.dj_gain_db -- the ONLY
        # place this ever hits the DB; _on_element_message's dj_level
        # handler reuses this cached value on every ~100ms meter
        # message rather than querying per-message.
        dj_gain_db = RemoteDJAudioInput.load().gain_db
        session.dj_gain_db = dj_gain_db
        slot.remote_gain.set_property("volume", 10 ** (dj_gain_db / 20.0))
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
        if DJ_DUMP_PCM_ENABLED:
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

        # Monitor-mixer timeline diagnostics -- see the mon_mixer
        # start-time-selection comment below for the bug this
        # instruments. `_mon_diag_wall_start` anchors the "elapsed wall
        # time to first monitor output buffer" figure logged by the
        # one-shot probes installed near the end of this method;
        # `pipeline_running_time_at_session_build` is the pipeline's
        # own running time AT THIS MOMENT, i.e. exactly what a
        # dynamically-created audiomixer's start-time-selection=ZERO
        # default would ignore (and start its own output segment at 0
        # instead of). Best-effort -- a clock query only ever fails if
        # main_pipeline has no clock at all, which would mean nothing
        # is playing anyway.
        _mon_diag_wall_start = time.time()
        _mon_diag_clock = self.main_pipeline.get_clock()
        if _mon_diag_clock:
            _mon_diag_running_time_ns = _mon_diag_clock.get_time() - self.main_pipeline.get_base_time()
            _dj_diag(
                session,
                f"monitor_mixer_diag pipeline_running_time_at_session_build="
                f"{_mon_diag_running_time_ns / Gst.SECOND:.3f}s",
            )

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
        # start-time-selection: FIRST, not the inherited default (ZERO).
        #
        # Unlike self.mixer / self.master_mixer / self.program_fx_mixer /
        # self.fx_submix -- all built once in _build_main_pipeline (and
        # _fx_setup, itself called from _build_main_pipeline) BEFORE
        # main_pipeline is ever set to PLAYING -- mon_mixer is created
        # HERE, dynamically, per Remote DJ connection, and added into a
        # pipeline that may already have been PLAYING for hours. Those
        # other mixers all start their own output segment at running-time
        # 0 together with everything else at engine startup, which is
        # correct for them; mon_mixer inheriting that same ZERO default
        # is not: GstAggregator's "zero" start-time-selection makes THIS
        # mixer declare its own output segment starting at running-time
        # 0 regardless of how long the pipeline has already been running,
        # but its real inputs -- remote_dj_tee / local_mic_tee, both
        # flowing continuously since engine startup -- arrive timestamped
        # in the pipeline's CURRENT running-time domain. The aggregator
        # then has to generate silence gap-fill to advance its own
        # zero-rooted output timeline up to the real, already-elapsed
        # running time before any real audio reaches WebRTC -- a
        # catch-up window that grows with engine uptime.
        #
        # Root-caused live 2026-08 (Remote DJ monitor return producing a
        # steadily growing stretch of pure-silence Opus RTP -- confirmed
        # via Firefox getStats(): totalAudioEnergy=0 despite tens of
        # thousands of packetsReceived, each a fixed 3 bytes -- an empty
        # Opus DTX/CNG-equivalent silence frame) and reproduced offline
        # in an isolated GstAggregator harness, not just inferred from
        # the live symptom (see
        # library/tests/test_remote_dj_monitor_mixer_timeline.py): with
        # the inherited ZERO default, a freshly-created audiomixer 2s
        # into a running parent pipeline's life emitted a PTS=0,
        # GAP-flagged (silent) first output buffer -- a ~2s misalignment
        # matching this bug's own signature exactly, scaled down from
        # hours to seconds. With FIRST, the same mixer's first output
        # buffer already carried real, non-silent signal at the correct
        # ~2s running time, no catch-up gap at all.
        #
        # FIRST -- not the newer NOW selection some GStreamer versions
        # also offer -- aligns the mixer's start time to when its first
        # real buffer actually arrives, which is the media-driven
        # behavior actually wanted here (NOW would instead use the wall
        # clock at mixer-creation time, a real but subtly different
        # thing), and FIRST is available on older, already-deployed
        # gst-plugins-base versions this project has to keep working on.
        mon_mixer.set_property("start-time-selection", GstBase.AggregatorStartTimeSelection.FIRST)
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
        #
        # Every pad link below goes through _link_monitor_pad rather
        # than a bare .link() call. PyGObject's own Gst.Pad.link()
        # override already raises gi.repository.Gst.LinkError whenever
        # the underlying Gst.PadLinkReturn isn't OK (verified against
        # the installed bindings: unlike the raw C API, a Python caller
        # here never gets a failed return value it could silently fail
        # to check) -- so a failed link here was ALREADY propagating as
        # an exception, and _remote_dj_session_start's existing
        # try/except around this whole method was ALREADY catching it
        # and rolling the session back safely (releasing the slot,
        # tearing down every element built so far, releasing the tee
        # pad if one was claimed -- see that method's rollback branch).
        # _link_monitor_pad below adds only a clear, this-link-specific
        # message in place of a bare `LinkError: -3` -- genuinely useful
        # for diagnosing which of these four links failed and why,
        # without changing whether a failure is caught or how rollback
        # happens.
        def _link_monitor_pad(src_pad, sink_pad, description):
            try:
                src_pad.link(sink_pad)
            except Gst.LinkError as exc:
                raise RuntimeError(
                    f"Remote DJ monitor-return link failed ({description}): "
                    f"{src_pad.get_name()!r} -> {sink_pad.get_name()!r}: {exc}"
                ) from exc

        # One-shot monitor-mixer timeline diagnostics -- see mon_mixer's
        # start-time-selection comment above for the bug this proves
        # stays fixed. Logs the first buffer's own PTS (this session's
        # audio is a simple, linear-rate live chain, so a buffer's own
        # PTS is a fair diagnostic stand-in for its running time --
        # diagnostic only, never used for a control decision) and, on
        # the mixer's own output, whether GstAggregator marked it a
        # synthesized GAP buffer -- the same flag confirmed present on
        # the reproduced pre-fix silence in
        # test_remote_dj_monitor_mixer_timeline.py. Fires once per pad
        # then removes itself; defensively wrapped like every other
        # Remote DJ diagnostic probe in this method -- a broken
        # diagnostic must never touch the audio path.
        def _install_first_buffer_diag_probe(pad, label):
            def _probe(probed_pad, info, _u):
                try:
                    s = self.remote_dj_session
                    if s is not session:
                        return Gst.PadProbeReturn.REMOVE
                    buf = info.get_buffer()
                    if buf is not None:
                        pts = buf.pts
                        pts_str = f"{pts / Gst.SECOND:.3f}s" if pts != Gst.CLOCK_TIME_NONE else "none"
                        is_gap = bool(buf.get_flags() & Gst.BufferFlags.GAP)
                        elapsed = time.time() - _mon_diag_wall_start
                        _dj_diag(
                            s,
                            f"monitor_mixer_diag {label} pts={pts_str} gap={is_gap} "
                            f"elapsed_wall_since_session_build={elapsed:.3f}s",
                        )
                except Exception as exc:
                    try:
                        _dj_diag(session, f"monitor_mixer_diag {label} probe DISABLED after exception: {exc!r}")
                    except Exception:
                        pass
                return Gst.PadProbeReturn.REMOVE
            pad.add_probe(Gst.PadProbeType.BUFFER, _probe, None)

        _install_first_buffer_diag_probe(mon_mixer.get_static_pad("src"), "mixer_output")

        session.monitor_tee_pad = self.remote_dj_tee.request_pad_simple("src_%u")
        _link_monitor_pad(session.monitor_tee_pad, mon_decks_q.get_static_pad("sink"), "remote_dj_tee -> mon_decks_q")
        mon_mixer_decks_sink = mon_mixer.request_pad_simple("sink_%u")
        _link_monitor_pad(mon_decks_q.get_static_pad("src"), mon_mixer_decks_sink, "mon_decks_q -> mon_mixer (decks)")
        _install_first_buffer_diag_probe(mon_mixer_decks_sink, "program_monitor_input")

        if self.local_mic_tee is not None:
            session.local_mic_tee_pad = self.local_mic_tee.request_pad_simple("src_%u")
            _link_monitor_pad(session.local_mic_tee_pad, mon_mic_q.get_static_pad("sink"), "local_mic_tee -> mon_mic_q")
            mon_mixer_mic_sink = mon_mixer.request_pad_simple("sink_%u")
            _link_monitor_pad(mon_mic_q.get_static_pad("src"), mon_mixer_mic_sink, "mon_mic_q -> mon_mixer (local mic)")
            _install_first_buffer_diag_probe(mon_mixer_mic_sink, "local_mic_monitor_input")

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
        # OFF by default (DJ_DUMP_PCM_ENABLED, see its own comment near
        # DJ_DUMP_PCM's definition) -- gated below and at the open()
        # call in _remote_dj_session_start, so none of this fires at
        # all while disabled.
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
        if DJ_DUMP_PCM_ENABLED:
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

        seek_ok = new_deck.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            int(resume_position * Gst.SECOND),
        )
        if seek_ok:
            new_deck.seeked_at = time.time()
        else:
            # _create_deck already set started_at as though playback
            # began at resume_position_ns -- that's presentation
            # bookkeeping based on the PARAMETER, not a real seek
            # _create_deck itself never performs. The actual seek is
            # this call, made by the caller (us) after _create_deck
            # returns; if GStreamer rejects it, the deck's real audio
            # is still wherever decodebin naturally started (position
            # 0), and started_at must reflect that or _get_deck_position
            # (dashboard, crossfade timing) would keep reporting the
            # unreached target indefinitely.
            print(f"  [{slot}] Resume seek to {resume_position:.1f}s rejected -- playing from 0 instead", flush=True)
            emit_event(
                category="engine", level="error", title="Deck seek rejected",
                detail={"slot": slot, "track_id": new_deck.track.id, "target_seconds": resume_position},
                dedupe_key=f"engine|seek-rejected|slot={slot}|track={new_deck.track.id}",
            )
            new_deck.started_at = time.time()

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

        seek_ok = new_deck.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            int(position * Gst.SECOND),
        )
        if seek_ok:
            new_deck.seeked_at = time.time()
        else:
            # Same reasoning as _resume_deck: _create_deck already set
            # started_at as though playback began at the target -- if
            # GStreamer rejects this seek, the deck's real audio is
            # still at position 0, and started_at must say so.
            print(f"  [{slot}] Seek to {position:.1f}s rejected -- playing from 0 instead", flush=True)
            emit_event(
                category="engine", level="error", title="Deck seek rejected",
                detail={"slot": slot, "track_id": new_deck.track.id, "target_seconds": position},
                dedupe_key=f"engine|seek-rejected|slot={slot}|track={new_deck.track.id}",
            )
            new_deck.started_at = time.time()

        with self._lock:
            self.decks[slot] = new_deck
        self._deck_bin_map[id(new_deck.pipeline)] = new_deck

        bus = new_deck.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_deck_error, new_deck)

        if was_paused:
            self._pause_deck(slot)
            if seek_ok:
                new_deck.paused_position = position
            # else: _pause_deck already derived paused_position from a
            # real _get_deck_position() query against the deck's TRUE
            # (unseeked, now-corrected-to-0) state -- don't clobber
            # that with the target the deck never actually reached.
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

    def _apply_audio_output_device(self, device, *, identity_changed=False):
        """Request an intentional Studio Monitor branch retarget.

        The admin command passes ``identity_changed=True`` when the stable
        identity fields changed. That is an explicit live retarget even when
        the raw ``device`` fallback is unchanged. A raw-path edit remains
        fallback-only while a stable identity is active.

        The actual lifecycle is the existing bounded output-slot lifecycle:
        close only this slot's valve, synchronously detach and retire the old
        generation, dispatch its potentially blocking NULL transition through
        SlotCoordinator, then build/verify/promote a fresh generation. The
        parent pipeline and StereoTool sibling are never state-cycled.

        Returning True means the asynchronous retarget was accepted, not that
        the target has already opened. Identity/generation epochs invalidate
        an older candidate if this request races an in-flight recovery.
        """
        if not self._studio_monitor_slot or not self.main_pipeline:
            return False
        slot = self._studio_monitor_slot
        raw_changed = device != slot.legacy_device
        if not raw_changed and not identity_changed:
            return False
        if raw_changed:
            slot.legacy_device = device
        if slot.identity_kind == "alsa_card_id" and slot.identity and not identity_changed:
            print(f"  Studio Monitor raw device changed ({device}) while a stable identity is "
                  f"active ({slot.identity_kind}={slot.identity!r}) -- recorded as fallback "
                  f"configuration only; the live sink stays on its stable identity path")
            return False

        runtime_device = audio_recovery.resolve_runtime_device(
            slot.identity_kind, slot.identity, slot.legacy_device)
        print(f"  Requesting branch-local Studio Monitor retarget -> {runtime_device}")

        # Invalidate any candidate already being verified. Its promotion
        # epoch check will discard it, so an identity edit racing recovery
        # can never promote a generation built for the previous identity.
        slot.invalidate_generation()
        slot.retarget_requested = True
        slot.retarget_in_progress = True
        slot.recovery_attempt = 0
        slot.next_retry_at = None
        slot.device_present = None
        slot.last_state_change_at = time.time()
        slot.valve.set_property("drop", True)

        if slot.current_bin is not None and slot.coordinator.state == audio_recovery.SlotState.OK:
            slot.coordinator.mark_degraded()
            self._output_quiesce_current_generation(slot)
            return True

        if slot.coordinator.state == audio_recovery.SlotState.OK:
            slot.coordinator.mark_degraded()
        if (slot.coordinator.state == audio_recovery.SlotState.DEGRADED and
                slot.pending_bin is None):
            slot.retarget_requested = False
            self._output_dispatch_rebuild(slot)
        else:
            print("  Studio Monitor retarget queued behind the slot's current bounded operation")
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
                else:
                    # Real next hour isn't built yet -- dispatch an
                    # async live-fill (1.1 spec: the DB work no longer
                    # runs inline on this thread, so nothing is
                    # available to pick up THIS tick; _install_live_fill
                    # extends self.log_items once the worker finishes,
                    # which a later poll tick's _peek_playable_at_cursor
                    # call picks up normally).
                    self._try_extend_live_log_async()

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
        # been applied.
        #
        # Deliberately scoped to a short window right after an ACTUAL
        # seek (deck.seeked_at), not the deck's whole lifetime -- an
        # unseeked deck's EOS is trusted exactly as it always was.
        # Reproduced this in isolation (throwaway pipeline, zero
        # connection to the live engine, same file/seek offset as the
        # incident): the parser assertion is 100% deterministic for
        # that seek, but the pipeline itself recovered cleanly and
        # resumed normal real-time decoding every single time (5/5
        # runs), settling within ~2.7s -- SEEK_EOS_GUARD_SECONDS. That
        # supports "ignore it" as reasonable for the reproduced case,
        # but the real incident's full pipeline topology (linked into
        # the live mixer, carrying this exact probe, under real
        # concurrent engine-startup load) wasn't and can't safely be
        # replicated in an isolated script -- if a deck genuinely goes
        # silent after this fires, that's the next thing to chase.
        recent_seek = deck.seeked_at is not None and (time.time() - deck.seeked_at) <= SEEK_EOS_GUARD_SECONDS
        if recent_seek:
            duration = deck.track.duration_seconds or 0
            if duration:
                # min(...) keeps the threshold from going negative (and
                # so silently disabling the whole check) for anything
                # shorter than DECK_STUCK_TIMEOUT_SECONDS -- station
                # IDs, sweepers, WxAlert/UrgentPA inserts, dedication
                # intros are ALL shorter than that 30s margin.
                margin = min(DECK_STUCK_TIMEOUT_SECONDS, duration / 2)
                pos = self._get_deck_position(deck)
                if pos < duration - margin:
                    print(f"  [{deck.slot}] Ignoring implausible post-seek EOS at {pos:.1f}s "
                          f"(duration {duration:.1f}s) on {deck.track.title!r} -- "
                          f"likely a post-seek parser hiccup, not a real end of stream")
                    emit_event(
                        category="engine", level="warning", title="Ignored implausible post-seek EOS",
                        detail={
                            "slot": deck.slot, "track_id": deck.track.id, "track_title": deck.track.title,
                            "position_seconds": round(pos, 1), "duration_seconds": round(duration, 1),
                            "seconds_since_seek": round(time.time() - deck.seeked_at, 1),
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
        hour_state = self._current_hour_schedule_state(now)
        if (
            not hour_state["schedule_expected"]
            and hour_state["active_key"] is not None
            and hour_state["active_key"] < hour_state["now_key"]
        ):
            # An intentional blank hour still belongs to the older active
            # log while fallback is being computed. Loading the nonexistent
            # wall-clock log here would clear current_log/log_items and make
            # the already-dispatched live-fill proposal stale by design.
            self._try_extend_live_log_async()
            print("Unscheduled hour exhausted. Waiting for live fill...")
            GLib.timeout_add_seconds(30, self._try_load_next_hour)
            return
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
        hour_state = self._current_hour_schedule_state(now)
        if (
            not hour_state["schedule_expected"]
            and hour_state["active_key"] is not None
            and hour_state["active_key"] < hour_state["now_key"]
        ):
            # Keep ownership on the older log throughout an intentional
            # blank hour. A completed live-fill extends this same queue; a
            # pending/failed fill is retried without invalidating its
            # generation. Once wall clock reaches an exactly scheduled hour,
            # this branch stops applying and normal current-hour loading wins.
            if self._queue_cursor < len(self.log_items):
                self._start_next_track()
                return False
            if hour_state["has_committed_playout"]:
                return False
            self._try_extend_live_log_async()
            return True
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

    def _leading_deck_eta_seconds(self, snapshot=None):
        """Seconds remaining before the SOONEST-to-finish currently-
        playing (unpaused) deck reaches its own crossfade/next-start
        trigger point -- i.e. how soon a new track could actually
        start. This is what _write_state uses to seed the queue's live
        ETA (see webrequests.services.estimate_air_time, the operator-
        facing "Starts at" estimate) and what clock-drift recovery
        (_project_upcoming_hour_target_duration) uses to project the
        upcoming hour's real takeover time -- both need the deck that
        actually governs the NEXT queue-start decision.

        Deliberately NOT the same tie-break as _leading_deck() (used
        elsewhere for position-polling/stuck-deck-watchdog purposes,
        where its own docstring notes "either [occupied unpaused deck]
        is fine" -- true THERE because _poll_position's trigger-point
        check is skipped entirely once both slots are occupied, so
        which one _leading_deck() picks during an overlap never
        actually matters to that caller). It matters here: during a
        brief crossfade overlap, the INCOMING deck (just started,
        nearly its full duration left) and the OUTGOING/finishing deck
        (about to free up) are both unpaused simultaneously, and only
        the finishing one governs when a NEW track could next start.
        Taking the MINIMUM projected ETA across every occupied,
        unpaused deck always selects the finishing one, regardless of
        which slot it happens to be in -- not "whichever is encountered
        first in SLOTS order", which could select the incoming deck.

        Fails safe to 0.0 (no projected lateness -- the caller falls
        back to a nominal full-hour target) on ANY unexpected error
        reading an individual deck (missing track, a pipeline query
        raising, etc.) rather than risk shortening an upcoming hour off
        an unreliable read; a bad reading from one deck doesn't discard
        a good reading from the other.

        `snapshot` lets a caller that already holds a fresh
        dict(self.decks) (like _write_state) pass it in and skip a
        redundant lock/copy; omitted, this takes its own snapshot.
        Returns 0.0 if no deck is currently eligible (both empty, or
        both paused -- a frozen/paused deck's remaining time is not a
        trustworthy prediction of when automation will resume, so
        paused decks are excluded from consideration entirely, same as
        _leading_deck())."""
        if snapshot is None:
            with self._lock:
                snapshot = dict(self.decks)

        etas = []
        for slot in SLOTS:
            d = snapshot[slot]
            if not d or d.paused:
                continue
            try:
                lt = d.track
                if lt is None:
                    continue
                lpos = self._get_deck_position(d)
                l_effective = lt.next_start_seconds if lt.next_start_seconds is not None else (lt.duration_seconds or 0)
                etas.append(max(0.0, l_effective - lpos))
            except Exception as exc:
                print(f"  _leading_deck_eta_seconds: failed reading deck {slot} (non-fatal, skipping): {exc}")
                continue

        if not etas:
            return 0.0
        return min(etas)

    def _project_upcoming_hour_target_duration(self, nominal_start):
        """Clock-drift recovery (1.1 spec): projects the REAL takeover
        time for the upcoming hour using the same authoritative
        projection semantics as the operator-facing "Starts at"
        estimate (see _leading_deck_eta_seconds) -- NOT the static
        nominal top-of-hour boundary. _advance_to_next_hour_log's queue
        swap only changes what plays NEXT; whatever's already playing
        on a deck finishes normally, so if THAT track's own crossfade
        trigger point falls after `nominal_start`, the new hour's first
        track won't actually start until then -- this is the real
        source of "clock drift" this feature compensates for, not the
        current hour somehow running past the top of the hour with
        unplayed content (the engine always forcibly cuts over at
        nominal_start regardless; see _advance_to_next_hour_log).

        late_offset_seconds = max(0, projected_start - nominal_start) --
        ONE-WAY only: an early projection (leading deck about to finish
        before nominal_start) never LENGTHENS the target, it's clamped
        to 0 first. Clamped to MAX_CLOCK_RECOVERY_SECONDS so a wildly-
        off projection (e.g. a stuck/paused deck) can't shrink the
        built hour to nothing; clamping is reported via emit_event so
        it's visible to an operator, not silently absorbed.

        Wrapped in a fail-safe try/except: ANY unexpected error in this
        projection (not just a per-deck read failure, which
        _leading_deck_eta_seconds already contains) falls back to
        NOMINAL_HOUR_SECONDS -- "do not shorten an upcoming hour using
        an unreliable projection" holds even against a bug in this
        function itself, not just the deck-reading helper it calls.

        Returns target_duration_seconds (NOMINAL_HOUR_SECONDS minus the
        clamped late_offset_seconds)."""
        try:
            eta_seconds = self._leading_deck_eta_seconds()
            now = timezone.localtime()
            projected_start = now + timedelta(seconds=eta_seconds)
            late_offset_seconds = max(0.0, (projected_start - nominal_start).total_seconds())

            if late_offset_seconds > MAX_CLOCK_RECOVERY_SECONDS:
                emit_event(
                    category="engine", level="warning", title="Clock-drift recovery offset clamped",
                    detail={
                        "nominal_start": nominal_start.isoformat(),
                        "projected_start": projected_start.isoformat(),
                        "unclamped_late_offset_seconds": round(late_offset_seconds, 1),
                        "clamped_to_seconds": MAX_CLOCK_RECOVERY_SECONDS,
                    },
                    dedupe_key=f"engine|clock-drift-clamped|{nominal_start.isoformat()}",
                )
                late_offset_seconds = MAX_CLOCK_RECOVERY_SECONDS

            return NOMINAL_HOUR_SECONDS - late_offset_seconds
        except Exception as exc:
            print(f"  Clock-drift projection failed (non-fatal, using nominal target): {exc}")
            emit_event(
                category="engine", level="error", title="Clock-drift projection failed",
                detail={"nominal_start": nominal_start.isoformat(), "exception": repr(exc), "traceback": traceback.format_exc()},
                dedupe_key=f"engine|clock-drift-projection-failed|{nominal_start.isoformat()}",
            )
            return NOMINAL_HOUR_SECONDS

    def _compute_queue_eta_state(self, snapshot):
        """Builds the live queue list with listener-audible-start
        eta_seconds/airtime_seconds for each upcoming item -- factored
        out of _write_state so it's directly testable without needing
        to stand up the rest of the engine (mic, remote DJ, FX fires,
        etc.). Feeds /run/isadoraair/engine_state.json's "queue" array,
        which the dashboard's Coming Up list and webrequests.services.
        estimate_air_time (via _live_eta_datetime) both read for their
        own "Starts at"/air-time estimates.

        1.1 airtime-correction follow-up -- listener-audible-start
        semantics, not raw file-position next_start_seconds:

        The FIRST offset (`eta`, below) is fundamentally different from
        every offset after it: it's the REMAINING runway on the deck
        that's already mid-playback right now, from its CURRENT source
        position forward to the next audible handoff.
        _leading_deck_eta_seconds already computes exactly that
        (next_start_current - current_position) -- the leading deck's
        own cue_in has already happened and played out, so it must NOT
        be subtracted again here (that would double-count it). This is
        precisely why _leading_deck_eta_seconds was correctly left
        unchanged by the airtime-correction pass; nothing about it
        needs to change for this fix either.

        Every SUBSEQUENT queued item, by contrast, is a full future
        track that hasn't started yet -- its contribution is the same
        listener-facing effective_airtime_seconds() log_builder.py's
        own build-time scheduling now uses (next_start_seconds -
        cue_in_seconds, with the same explicit is-not-None/duration-
        fallback semantics), reused here rather than re-implemented, so
        the two can never silently drift apart again. Previously this
        loop computed the correct cue-in-adjusted value into a local
        for the per-item DISPLAY field but advanced the running total
        with the UNADJUSTED raw value instead -- silently overcounting
        every future queued track's contribution to "Starts at" by its
        own cue-in, even though the per-item "airtime_seconds" figure
        right next to it was already correct. Fixed: both now use the
        same value."""
        eta = self._leading_deck_eta_seconds(snapshot=snapshot)

        queue = []
        for qi in self._get_upcoming_preview():
            qt = qi.track
            # Same number the countdown on the Playing deck decays
            # down from once this track goes live, so the preview
            # deck UI shows the identical figure to prime the DJ.
            q_airtime = effective_airtime_seconds(qt)
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
            eta += q_airtime
        return queue

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
            # show a live "time on" clock estimate -- listener-audible-
            # start semantics; see _compute_queue_eta_state's own
            # docstring for the full derivation (1.1 airtime-correction
            # follow-up) and why the current-deck and future-queue
            # offsets use deliberately different formulas.
            queue = self._compute_queue_eta_state(snapshot)

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
                # [P0] 1.3B2 -- input hotplug recovery observability.
                # None throughout when no mic is configured at all
                # (self._mic_slot stays None -- see _build_mic_chain).
                "mic_recovery": self._mic_recovery_state(),
                # [P0] 1.3C -- output hotplug recovery observability,
                # keyed by slot kind ("studio_monitor"/"stereotool"). {}
                # if no output slots exist at all (shouldn't happen --
                # Studio Monitor's slot is always built).
                "output_recovery": self._output_recovery_state(),
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
                # Authoritative FX Cart playback state -- see
                # _fx_fires_state's own docstring. Drives the dashboard's
                # progress-bar reconciliation regardless of what
                # initiated the fire (manual click, keyboard shortcut,
                # another open dashboard/remote-DJ session, or an
                # external trigger like the weather beep bridge).
                "fx_fires": self._fx_fires_state(),
                # 1.7 release/version-skew visibility -- fixed at process
                # start (see __init__), not re-derived here. None if git
                # was unavailable when this process started.
                "runtime_commit": self._runtime_commit,
            }

            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.rename(STATE_PATH)
        except Exception as exc:
            print(f"Failed to write state: {exc}")
