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
        self._deck_bin_map = {}
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
            self._start_track(0, as_leading=True)

        self._position_timer = GLib.timeout_add(POSITION_POLL_MS, self._poll_position)

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
        for deck in self.decks:
            if deck.pipeline:
                deck.pipeline.set_state(Gst.State.NULL)
        if self.loop.is_running():
            self.loop.quit()
        self._write_state(transport="STOPPED")
        print("Engine stopped.")

    def _handle_signal_glib(self):
        print("Shutting down...")
        self.loop.quit()
        return GLib.SOURCE_REMOVE

    def _build_main_pipeline(self):
        self.main_pipeline = Gst.Pipeline.new("isadoraair")

        self.mixer = Gst.ElementFactory.make("audiomixer", "mixer")
        self.alsasink = Gst.ElementFactory.make("alsasink", "output")
        self.alsasink.set_property("device", "plughw:1,0")

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
            .select_related("track", "track__artist", "track__album", "track__category")
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
            self._deck_bin_map.pop(id(deck.pipeline), None)
            deck.pipeline.set_state(Gst.State.NULL)
            deck.pipeline.get_static_pad("src").unlink(deck.mixer_pad)
            self.mixer.release_request_pad(deck.mixer_pad)
            self.main_pipeline.remove(deck.pipeline)
            deck.finished = True
            if deck in self.decks:
                self.decks.remove(deck)

    def _start_track(self, index, as_leading=False):
        if index >= len(self.log_items):
            self._on_log_exhausted()
            return

        if as_leading:
            self.current_index = index

        log_item = self.log_items[index]
        deck = self._create_deck(log_item)

        if deck is None:
            if as_leading:
                self._start_track(index + 1, as_leading=True)
            return

        self._deck_bin_map[id(deck.pipeline)] = deck

        with self._lock:
            self.decks.append(deck)

        bus = deck.pipeline.get_bus()
        bus.add_signal_watch()
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
            if not self.decks:
                self._write_state()
                return True
            leading_deck = self.decks[0]

        if leading_deck.finished:
            self._write_state()
            return True

        # Don't check trigger until deck has been playing for at least 5 seconds
        deck_age = time.time() - (leading_deck.started_at or time.time())
        if deck_age < 5.0:
            self._write_state()
            return True

        pos = self._get_deck_position(leading_deck)
        track = leading_deck.track

        next_start = track.next_start_seconds
        if next_start is None:
            next_start = track.duration_seconds or 0

        # Sanity: position must be reasonable (> 5s, < track duration + buffer)
        max_pos = (track.duration_seconds or 3600) + 10
        if pos <= 5.0 or pos > max_pos:
            self._write_state()
            return True

        next_index = self.current_index + 1
        if next_index < len(self.log_items):
            next_item = self.log_items[next_index]
            next_cue_in = next_item.track.cue_in_seconds or 0.0
            trigger_point = next_start - next_cue_in

            if trigger_point < 10.0:
                trigger_point = next_start

            with self._lock:
                already_started = any(
                    d.log_item.id == next_item.id for d in self.decks
                )

            if not already_started and pos >= trigger_point:
                print(f"  Trigger: pos={pos:.1f}s >= trigger={trigger_point:.1f}s, starting next")
                self._start_track(next_index)

        self._write_state()
        return True

    def _on_deck_eos_probed(self, deck_bin):
        deck = self._deck_bin_map.get(id(deck_bin))
        if not deck or deck.finished:
            return
        is_leading = self.decks and self.decks[0] is deck
        self._handle_deck_finished(deck, is_leading)

    def _on_deck_error(self, bus, message, deck):
        err, debug = message.parse_error()
        print(f"  GStreamer error on {deck.track.title}: {err} ({debug})")
        is_leading = self.decks and self.decks[0] is deck
        GLib.idle_add(self._handle_deck_finished, deck, is_leading)
        return True

    def _handle_deck_finished(self, deck, was_leading):
        self._remove_deck(deck)
        if was_leading:
            next_idx = self.current_index + 1
            with self._lock:
                already_playing = any(
                    d.log_item.position == self.log_items[next_idx].position
                    for d in self.decks
                ) if next_idx < len(self.log_items) and self.decks else False

            if already_playing:
                self.current_index = next_idx
            else:
                self._start_track(next_idx, as_leading=True)

    def _on_log_exhausted(self):
        print("Log exhausted for this hour.")
        now = timezone.localtime()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        self._load_log_for(next_hour.date(), next_hour.hour)
        if self.log_items:
            self._start_track(0, as_leading=True)
        else:
            print("No approved log for next hour. Waiting...")
            GLib.timeout_add_seconds(30, self._try_load_next_hour)

    def _try_load_next_hour(self):
        if not self.running:
            return False
        now = timezone.localtime()
        self._load_log_for(now.date(), now.hour)
        if self.log_items:
            self._start_track(0, as_leading=True)
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
                    "album": t.album.title if t.album else "",
                    "position": round(pos, 1),
                    "duration": t.duration_seconds or 0,
                    "next_start": t.next_start_seconds,
                    "cue_in": t.cue_in_seconds or 0,
                    "category": t.category.code if t.category else "",
                }

            next_index = self.current_index + 1
            if next_index < len(self.log_items):
                nt = self.log_items[next_index].track
                next_up = {
                    "track_id": nt.id,
                    "title": nt.title,
                    "artist": nt.artist.name if nt.artist else "",
                    "album": nt.album.title if nt.album else "",
                    "duration": nt.duration_seconds or 0,
                    "category": nt.category.code if nt.category else "",
                }

            queue = []
            for i in range(next_index + 1, min(next_index + 10, len(self.log_items))):
                qt = self.log_items[i].track
                queue.append({
                    "track_id": qt.id,
                    "title": qt.title,
                    "artist": qt.artist.name if qt.artist else "",
                    "duration": qt.duration_seconds or 0,
                    "category": qt.category.code if qt.category else "",
                })

            state = {
                "transport": transport,
                "now_playing": now_playing,
                "next_up": next_up,
                "queue": queue,
                "current_index": self.current_index,
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
