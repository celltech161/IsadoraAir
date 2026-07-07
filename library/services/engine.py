import json
import os
import signal
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "isadoraair.settings")
django.setup()

from django.db import close_old_connections
from django.utils import timezone
from hardware.models import AudioOutput, AudioPipeline
from library.models import LogItem, PlaylistLog, Track
from library.services.log_builder import (
    DURATION_FIT_MARGIN,
    append_fill_items,
    build_hour_log,
    fill_remaining_hour,
)

STUDIO_MONITOR_NAME = "Studio Monitor"
STUDIO_MONITOR_FALLBACK_DEVICE = "plughw:2,0"

# StereoTool bridge — a raw (pre-AGC) tap off the mixer, fed to an ALSA
# loopback device StereoTool reads from separately. Unlike Studio Monitor,
# there's no fallback device: if this row has nothing configured, the tee
# branch simply isn't built at all (see _build_main_pipeline).
STEREOTOOL_OUTPUT_NAME = "Stereotool Input"

STATE_PATH = Path("/run/isadoraair/engine_state.json")
CMD_PATH = Path("/run/isadoraair/engine_cmd.json")
NOW_PLAYING_PATH = Path("/run/isadoraair/now_playing.json")
POSITION_POLL_MS = 500
AUTO_BUILD_CHECK_SECONDS = 10
NEXT_HOUR_LOOKAHEAD_SECONDS = 30
CACHE_WARM_LEAD_SECONDS = 3.0
SILENCE_PRIME_SECONDS = 0.3
SLOTS = ("A", "B")


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

        # Interim leveling for the studio monitor only (StereoTool will
        # handle real transmitter processing separately, elsewhere). Kept
        # permanently in the chain rather than conditionally linked, so
        # enabling/disabling is just a property change, never a pipeline
        # topology change — see _apply_agc_config.
        self.agc_dynamic = Gst.ElementFactory.make("audiodynamic", "agc_dynamic")
        self.agc_makeup = Gst.ElementFactory.make("volume", "agc_makeup")
        self.agc_limiter = Gst.ElementFactory.make("rglimiter", "agc_limiter")

        elements = [
            self.mixer, convert, resample, capsfilter,
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

        for el in elements:
            self.main_pipeline.add(el)

        self.mixer.link(convert)
        convert.link(resample)
        resample.link(capsfilter)

        if stereotool_tee:
            capsfilter.link(stereotool_tee)
            stereotool_tee.link(self.agc_dynamic)
            stereotool_tee.link(stereotool_queue)
            stereotool_queue.link(self.stereotool_sink)
        else:
            capsfilter.link(self.agc_dynamic)

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
        self._queue_cursor = 0
        print(f"Loaded log for {target_date} {hour:02d}:00 — {len(self.log_items)} items")

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
        # existing for the current hour.
        with self._lock:
            idle = self.decks["A"] is None and self.decks["B"] is None
        if idle:
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
        except Exception as exc:
            print(f"  Command error: {exc}")

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

        new_cursor = 0
        for i, item in enumerate(fresh_items):
            if item.id in occupied_ids:
                new_cursor = i + 1

        self.log_items = fresh_items
        self._queue_cursor = new_cursor

    def _poll_position(self):
        if not self.running:
            return False

        self._check_commands()
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
            }

            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.rename(STATE_PATH)
        except Exception as exc:
            print(f"Failed to write state: {exc}")
