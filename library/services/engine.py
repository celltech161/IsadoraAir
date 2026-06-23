import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "isadoraair.settings")
django.setup()

from django.utils import timezone
from library.models import LogItem, PlaylistLog, Track

STATE_PATH = Path("/run/isadoraair/engine_state.json")
POSITION_POLL_MS = 500


class Deck:
    def __init__(self, track, log_item, pipeline, mixer_pad):
        self.track = track
        self.log_item = log_item
        self.pipeline = pipeline
        self.mixer_pad = mixer_pad
        self.started_at = None
        self.finished = False


class PlaybackEngine:
    def __init__(self):
        Gst.init(None)
        self.loop = GLib.MainLoop()
        self.mixer = None
        self.alsasink = None
        self.main_pipeline = None
        self.decks = []
        self.current_log = None
        self.log_items = []
        self.current_index = 0
        self.running = False
        self._position_timer = None
        self._lock = threading.Lock()

    def start(self):
        self.running = True
        self._build_main_pipeline()
        self._load_current_hour_log()

        if not self.log_items:
            print("No approved log for current hour. Waiting...")
        else:
            self._start_track(0)

        self._position_timer = GLib.timeout_add(POSITION_POLL_MS, self._poll_position)

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        print("Engine started.")
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        self.running = False
        if self.main_pipeline:
            self.main_pipeline.set_state(Gst.State.NULL)
        for deck in self.decks:
            if deck.pipeline:
                deck.pipeline.set_state(Gst.State.NULL)
        if self.loop.is_running():
            self.loop.quit()
        self._write_state(transport="STOPPED")
        print("Engine stopped.")

    def _handle_signal(self, signum, frame):
        print(f"Received signal {signum}, shutting down...")
        GLib.idle_add(self.loop.quit)

    def _build_main_pipeline(self):
        self.main_pipeline = Gst.Pipeline.new("isadoraair")

        self.mixer = Gst.ElementFactory.make("audiomixer", "mixer")
        self.alsasink = Gst.ElementFactory.make("alsasink", "output")

        convert = Gst.ElementFactory.make("audioconvert", "outconvert")
        resample = Gst.ElementFactory.make("audioresample", "outresample")
        capsfilter = Gst.ElementFactory.make("capsfilter", "outcaps")
        caps = Gst.Caps.from_string("audio/x-raw,rate=48000,channels=2")
        capsfilter.set_property("caps", caps)

        for el in [self.mixer, convert, resample, capsfilter, self.alsasink]:
            self.main_pipeline.add(el)

        self.mixer.link(convert)
        convert.link(resample)
        resample.link(capsfilter)
        capsfilter.link(self.alsasink)

        self.main_pipeline.set_state(Gst.State.PLAYING)

    def _load_current_hour_log(self):
        now = timezone.localtime()
        self._load_log_for(now.date(), now.hour)

    def _load_log_for(self, target_date, hour):
        log = (
            PlaylistLog.objects
            .filter(date=target_date, hour=hour, status="approved")
            .first()
        )
        if not log:
            self.current_log = None
            self.log_items = []
            self.current_index = 0
            return

        self.current_log = log
        self.log_items = list(
            log.items
            .select_related("track", "track__artist")
            .order_by("position")
        )
        self.current_index = 0
        print(f"Loaded log for {target_date} {hour:02d}:00 — {len(self.log_items)} items")

    def _create_deck(self, log_item):
        track = log_item.track
        filepath = track.filepath

        if not Path(filepath).is_file():
            print(f"  File not found: {filepath}")
            return None

        src = Gst.ElementFactory.make("filesrc", None)
        src.set_property("location", filepath)

        decode = Gst.ElementFactory.make("decodebin", None)
        convert = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)

        bin_name = f"deck_{log_item.id}"
        deck_bin = Gst.Bin.new(bin_name)
        deck_bin.add(src)
        deck_bin.add(decode)
        deck_bin.add(convert)
        deck_bin.add(resample)

        src.link(decode)
        convert.link(resample)

        ghost_pad = Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC)
        deck_bin.add_pad(ghost_pad)

        def on_pad_added(element, pad):
            if pad.get_current_caps():
                struct = pad.get_current_caps().get_structure(0)
                if struct.get_name().startswith("audio"):
                    pad.link(convert.get_static_pad("sink"))
                    ghost_pad.set_target(resample.get_static_pad("src"))

        decode.connect("pad-added", on_pad_added)

        self.main_pipeline.add(deck_bin)

        mixer_pad = self.mixer.request_pad_simple("sink_%u")
        deck_bin.get_static_pad("src").link(mixer_pad)

        deck_bin.sync_state_with_parent()

        deck = Deck(
            track=track,
            log_item=log_item,
            pipeline=deck_bin,
            mixer_pad=mixer_pad,
        )
        deck.started_at = time.time()

        log_item.played_at = timezone.now()
        log_item.save(update_fields=["played_at"])

        Track.objects.filter(id=track.id).update(
            last_played_at=timezone.now(),
            play_count=track.play_count + 1,
        )

        print(f"  Playing: {track.artist.name if track.artist else '?'} - {track.title}")
        return deck

    def _remove_deck(self, deck):
        with self._lock:
            deck.pipeline.set_state(Gst.State.NULL)
            deck.pipeline.get_static_pad("src").unlink(deck.mixer_pad)
            self.mixer.release_request_pad(deck.mixer_pad)
            self.main_pipeline.remove(deck.pipeline)
            deck.finished = True
            if deck in self.decks:
                self.decks.remove(deck)

    def _start_track(self, index):
        if index >= len(self.log_items):
            self._on_log_exhausted()
            return

        self.current_index = index
        log_item = self.log_items[index]
        deck = self._create_deck(log_item)

        if deck is None:
            self._start_track(index + 1)
            return

        with self._lock:
            self.decks.append(deck)

        bus = deck.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_deck_eos, deck)
        bus.connect("message::error", self._on_deck_error, deck)

    def _get_deck_position(self, deck):
        ok, position = deck.pipeline.query_position(Gst.Format.TIME)
        if ok:
            return position / Gst.SECOND
        if deck.started_at:
            return time.time() - deck.started_at
        return 0.0

    def _poll_position(self):
        if not self.running:
            return False

        with self._lock:
            active_decks = list(self.decks)

        for deck in active_decks:
            if deck.finished:
                continue

            pos = self._get_deck_position(deck)
            track = deck.track

            next_start = track.next_start_seconds
            if next_start is None:
                next_start = track.duration_seconds or 0

            next_index = self.current_index + 1
            if next_index < len(self.log_items):
                next_item = self.log_items[next_index]
                next_cue_in = next_item.track.cue_in_seconds or 0.0
                trigger_point = next_start - next_cue_in

                already_started = any(
                    d.log_item.id == next_item.id for d in self.decks
                )

                if not already_started and pos >= trigger_point and trigger_point > 0:
                    self._start_track(next_index)

        self._write_state()
        return True

    def _on_deck_eos(self, bus, message, deck):
        GLib.idle_add(self._remove_deck, deck)
        return True

    def _on_deck_error(self, bus, message, deck):
        err, debug = message.parse_error()
        print(f"  GStreamer error on {deck.track.title}: {err} ({debug})")
        GLib.idle_add(self._remove_deck, deck)
        if deck.log_item == self.log_items[self.current_index]:
            GLib.idle_add(self._start_track, self.current_index + 1)
        return True

    def _on_log_exhausted(self):
        print("Log exhausted for this hour.")
        now = timezone.localtime()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        self._load_log_for(next_hour.date(), next_hour.hour)
        if self.log_items:
            self._start_track(0)
        else:
            print("No approved log for next hour. Waiting...")
            GLib.timeout_add_seconds(30, self._try_load_next_hour)

    def _try_load_next_hour(self):
        if not self.running:
            return False
        now = timezone.localtime()
        self._load_log_for(now.date(), now.hour)
        if self.log_items:
            self._start_track(0)
            return False
        return True

    def _write_state(self, transport="PLAYING"):
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

            now_playing = None
            next_up = None

            with self._lock:
                active_decks = list(self.decks)

            if active_decks:
                current = active_decks[0]
                pos = self._get_deck_position(current)
                t = current.track
                now_playing = {
                    "track_id": t.id,
                    "title": t.title,
                    "artist": t.artist.name if t.artist else "",
                    "position": round(pos, 1),
                    "duration": t.duration_seconds or 0,
                    "next_start": t.next_start_seconds,
                }

            next_index = self.current_index + 1
            if next_index < len(self.log_items):
                nt = self.log_items[next_index].track
                next_up = {
                    "track_id": nt.id,
                    "title": nt.title,
                    "artist": nt.artist.name if nt.artist else "",
                    "duration": nt.duration_seconds or 0,
                }

            state = {
                "transport": transport,
                "now_playing": now_playing,
                "next_up": next_up,
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
