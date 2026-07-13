"""RBDS engine -- reads Track/now-playing state and RBDSConfig/
RBDSPSFrame/RBDSMessage rows, and sends PS/RT (+ other static RDS
fields) to StereoTool's RDS encoder over UECP or ASCII, via TCP or UDP.

Follows monitoring/services/monitor.py's reactive time.sleep() loop
shape (not encoders/services/encoder_manager.py's subprocess-supervision
shape -- there's no subprocess here, this process itself holds the one
persistent connection)."""
import datetime
import json
import socket
import time
from pathlib import Path

import django

django.setup()

import requests  # noqa: E402
from django.db import close_old_connections  # noqa: E402

from rbds.models import RBDSConfig, RBDSMessage, RBDSPSFrame  # noqa: E402
from rbds.services import ascii_protocol, uecp  # noqa: E402
from rbds.services.content_fetch import ContentFetchCache  # noqa: E402
from rbds.services.rotation import PSRotation, RTRotation  # noqa: E402

POLL_SECONDS = 1
FULL_RESEND_SECONDS = 30  # periodic full resend even if nothing changed -- RDS receivers can miss packets
# RT+ tags need to be re-transmitted many times per RT slot: on-air RT+
# rides on 11A groups whose cadence in StereoTool's group sequence is
# much slower than 2A (the plain RT group), so a single UECP send of new
# tags at RT-change time will land at the encoder before StereoTool's
# next 11A slot -- but the receiver has already latched the new RT off
# 2A and is applying the OLD tags to it. RDS Magic 4's live capture
# emits 6-8 RT+ frame repeats per ~13s RT rotation slot; matching that
# cadence here (~6 repeats over 13s) collapses the observed
# stale-tag-on-new-RT window on TEF6686 / Uconnect receivers.
RT_PLUS_RESEND_SECONDS = 2
TCP_RECONNECT_BACKOFF = [1, 2, 5, 10, 30]
FILE_URL_FETCH_TIMEOUT = 5  # seconds, bounds a slow/hanging URL source's delay on the tick it fires

STATE_PATH = Path("/run/isadoraair/rbds_state.json")
NOW_PLAYING_PATH = Path("/run/isadoraair/now_playing.json")  # read-only, owned by library/services/engine.py


class RBDSManager:
    def __init__(self):
        self.running = False
        self._ps_rotation = PSRotation()
        self._rt_rotation = RTRotation()
        self._content_cache = ContentFetchCache()
        self._sock = None
        self._sqc = 0
        self._last_connect_attempt = 0.0
        self._backoff_index = 0
        self._last_sent_ps = None
        self._last_sent_rt = None
        self._rt_ab_flag = False
        self._last_full_resend = 0.0
        self._last_rt_plus_resend = 0.0
        self._last_error = None
        self._connected = False
        self._connected_since = None
        self._down_since = None
        self._last_now_playing = {"title": "", "artist": ""}
        # Last UTC minute we sent a CT (Clock Time) frame in, so we
        # send exactly one CT per minute (per RBDS spec: group 4A must
        # be transmitted at :00 seconds of the given minute). Reset to
        # None on init so the very first tick fires an immediate CT.
        self._last_ct_sent_minute = None

    def start(self):
        self.running = True
        close_old_connections()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

        import signal
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        print("RBDS engine started.")
        while self.running:
            try:
                self._tick()
            except Exception as exc:
                # Never let a bad tick kill the whole engine -- same
                # survival model as MonitorManager's probe exceptions.
                self._last_error = str(exc)
            time.sleep(POLL_SECONDS)
        self.stop()

    def stop(self):
        self.running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        print("RBDS engine stopped.")

    def _handle_signal(self, signum, frame):
        print("\nShutting down...")
        self.running = False

    # --- Main tick ---

    def _tick(self):
        close_old_connections()
        config = RBDSConfig.load()
        now_playing = self._read_now_playing()

        # Re-read fresh every tick, same as RBDSMessage/RBDSPSFrame below --
        # no engine restart needed for an admin edit to take effect.
        self._rt_rotation._nowplaying_min_seconds = config.nowplaying_min_seconds

        ps_frames = list(
            RBDSPSFrame.objects.filter(enabled=True).order_by("sort_order").values_list("text", "hold_seconds")
        )
        target_ps = self._ps_rotation.advance(ps_frames) or config.station_ps or ""

        today = datetime.date.today()
        messages = list(RBDSMessage.objects.filter(enabled=True).order_by("sort_order"))
        active_messages = [m for m in messages if m.is_active_today(today)]
        active_promo_tuples = [(m.name, m.display_seconds) for m in active_messages]
        messages_by_name = {m.name: m for m in active_messages}

        rt_source, rt_source_name = self._rt_rotation.advance(active_promo_tuples)

        rt_text, rt_artist, rt_title = self._resolve_rt_content(
            config, now_playing, rt_source, rt_source_name, messages_by_name,
        )

        changed = target_ps != self._last_sent_ps or rt_text != self._last_sent_rt
        due_for_full_resend = (time.time() - self._last_full_resend) >= FULL_RESEND_SECONDS

        if rt_text != self._last_sent_rt:
            self._rt_ab_flag = not self._rt_ab_flag

        # While disconnected, retry every tick rather than waiting for the
        # 30s full-resend window -- _ensure_tcp_connected()'s own
        # TCP_RECONNECT_BACKOFF (1/2/5/10/30s) is what actually paces the
        # real connect attempts; that backoff is dead code if this outer
        # gate only lets _send() run once per 30s regardless of connection
        # state; a failed attempt still stamps _last_full_resend below,
        # which used to reset the 30s window on every failure and starve
        # the fast early retries entirely. Confirmed live: a plain
        # `systemctl restart isadoraair-rbds` issued while StereoTool's
        # own TCP listener was still mid-startup left RBDS stuck for well
        # over a minute -- a second restart happened to land after
        # StereoTool was ready and "fixed" it, which looked like the admin
        # save mattered but was really just a lucky retry window.
        if changed or due_for_full_resend or not self._connected:
            self._send(config, target_ps, rt_text, rt_artist, rt_title)
            self._last_sent_ps = target_ps
            self._last_sent_rt = rt_text
            self._last_full_resend = time.time()
            # A full send just went out and includes the RT+ MECs, so
            # count it as a fresh RT+ resend and let the 2s timer start
            # counting from here rather than immediately re-firing.
            self._last_rt_plus_resend = time.time()

        # RT+ tag maintenance: on 2s cadence between full-sends, re-emit
        # only the RT+ MECs (ODA reg + tags + song info). Matches RDS
        # Magic 4's live capture (6-8 tag repeats per ~13s RT rotation
        # slot) so StereoTool's 11A group cadence has enough fresh
        # material to keep the receiver's RT+ display in sync with the
        # currently-airing RT. Gated on the same conditions the main
        # send uses -- no point sending RT+ if we can't reach StereoTool
        # or if the operator has RT+ off, and no point on ASCII where
        # RT+ rides RT+= in the ASCII payload rather than these MECs.
        due_for_rt_plus = (time.time() - self._last_rt_plus_resend) >= RT_PLUS_RESEND_SECONDS
        if (config.use_rt_plus and config.protocol == "uecp"
                and self._connected and due_for_rt_plus):
            try:
                self._send_rt_plus_only(config, rt_text, rt_artist, rt_title)
                self._last_rt_plus_resend = time.time()
            except Exception as exc:
                # RT+ resend is best-effort -- a failure here shouldn't
                # trip the connection state or drown _last_error the way
                # a main-payload failure would. Surface it quietly.
                self._last_error = f"RT+ resend failed: {exc}"

        # Dedicated CT send at minute boundaries -- see _send_ct
        # docstring for why this can't ride along with the content
        # payload above.
        if config.protocol == "uecp" and config.send_ct:
            current_minute = datetime.datetime.now(datetime.timezone.utc).minute
            if current_minute != self._last_ct_sent_minute:
                try:
                    self._send_ct(config)
                    self._last_ct_sent_minute = current_minute
                except Exception as exc:
                    # CT is auxiliary -- don't take the tick down or
                    # flip _connected over an isolated CT failure.
                    # Bump _last_error so the state file surfaces the
                    # failure without hiding the connection state.
                    self._last_error = f"CT send failed: {exc}"

        self._write_state(config, target_ps, rt_text, rt_source, rt_source_name)

    RT_PLUS_SEPARATOR = " - "

    def _resolve_rt_content(self, config, now_playing, rt_source, rt_source_name, messages_by_name):
        """Returns (rt_text, artist_or_none, title_or_none). artist/title
        are only returned non-None when RT+ tagging is actually going to
        be used for this content -- in that case rt_text is built via a
        FIXED "artist - title" join (RT_PLUS_SEPARATOR) so
        _build_ascii_payload() can compute exact tag offsets, rather than
        searching for substrings in an arbitrary user-configured
        now_playing_format template (which can't safely guarantee
        positions). now_playing_format is only honored when RT+ isn't
        in play."""
        if rt_source == "nowplaying":
            title = now_playing.get("title", "") or ""
            artist = now_playing.get("artist", "") or ""
        else:
            message = messages_by_name.get(rt_source_name)
            if message is None:
                return "", None, None
            raw_text = self._resolve_message_text(message)
            if message.rt_plus_delimiter and message.rt_plus_delimiter in raw_text:
                artist, _, title = raw_text.partition(message.rt_plus_delimiter)
                artist, title = artist.strip(), title.strip()
            else:
                return raw_text[:64], None, None

        if not artist:
            return title[:64], None, None

        if config.use_rt_plus:
            # Same "Artist - Title" join for both protocols now. On
            # ASCII this feeds the RT+= tag-offset math (unchanged);
            # on UECP the artist/title get sent as a separate MEC 0xAA
            # (StereoTool vendor "song info") in _build_uecp_payload,
            # populating StereoTool's internal Artist=/Title=/Song=
            # fields which its own RT+ generator reads from.
            text = f"{artist}{self.RT_PLUS_SEPARATOR}{title}"[:64]
            return text, artist, title

        if rt_source == "nowplaying":
            try:
                text = config.now_playing_format.format(title=title, artist=artist)
            except (KeyError, IndexError):
                text = title
        else:
            text = f"{artist}{self.RT_PLUS_SEPARATOR}{title}"
        return text[:64], None, None

    def _resolve_message_text(self, message):
        if message.source_type == "static":
            return message.text or ""
        if message.source_type == "file":
            def fetch():
                return Path(message.file_path).read_text(encoding="utf-8").strip()
            return self._content_cache.get(f"msg:{message.id}", message.poll_interval_seconds, fetch)
        if message.source_type == "url":
            def fetch():
                resp = requests.get(message.source_url, timeout=FILE_URL_FETCH_TIMEOUT)
                resp.raise_for_status()
                return resp.text.strip()
            return self._content_cache.get(f"msg:{message.id}", message.poll_interval_seconds, fetch)
        return ""

    def _read_now_playing(self):
        try:
            data = json.loads(NOW_PLAYING_PATH.read_text(encoding="utf-8"))
            self._last_now_playing = data
        except (OSError, ValueError):
            # Missing file, or a rare torn read of the non-atomic writer
            # (see library/services/engine.py's _write_now_playing) --
            # keep the previous tick's last-good value, retry next tick.
            pass
        return self._last_now_playing

    # --- Sending ---

    def _send(self, config, ps, rt, artist, title):
        try:
            if config.protocol == "uecp":
                payload = self._build_uecp_payload(config, ps, rt, artist, title)
            else:
                payload = self._build_ascii_payload(config, ps, rt, artist, title)
            self._transmit(config, payload)
        except Exception as exc:
            self._last_error = str(exc)
            self._mark_down()
            return
        self._mark_up()
        self._last_error = None

    def _send_rt_plus_only(self, config, rt, artist, title):
        """Small UECP frame carrying only the RT+ MECs -- fired between
        full sends to keep StereoTool's 11A group cadence saturated.
        See _tick's RT_PLUS_RESEND_SECONDS block for the why."""
        self._sqc = (self._sqc % 255) + 1
        msg = uecp.mec_rt_plus_oda_reg()
        if artist and title and len(artist) <= 32:
            msg += uecp.mec_rt_plus_tags(len(artist), len(title))
            msg += uecp.mec_song_info(f"{artist}{self.RT_PLUS_SEPARATOR}{title}")
        elif rt:
            msg += uecp.mec_rt_plus_tags_generic(len(rt))
        else:
            return  # nothing to (re-)send
        frame = uecp.build_frame(
            config.uecp_site_address, config.uecp_encoder_address, self._sqc, msg,
        )
        self._transmit(config, frame)

    def _build_uecp_payload(self, config, ps, rt, artist=None, title=None):
        self._sqc = (self._sqc % 255) + 1
        msg = b""
        if config.pi_code:
            msg += uecp.mec_pi(int(config.pi_code, 16))
        msg += uecp.mec_ps(ps)
        msg += uecp.mec_ta_tp(ta=config.ta, tp=config.tp)
        msg += uecp.mec_di(
            dynamic_pty=config.di_dynamic_pty, compressed=config.di_compressed,
            artificial_head=config.di_artificial_head, stereo=config.di_stereo,
        )
        msg += uecp.mec_ms(music=config.ms)
        msg += uecp.mec_pty(config.pty)
        msg += uecp.mec_rt(rt, ab_flag=self._rt_ab_flag)
        # RT+ over UECP -- reverse-engineered from a real RDS Magic 4 ->
        # StereoTool capture (see the scratchpad in the RT+ session
        # notes). Three MECs go together and are only emitted when we
        # have both artist and title:
        #   * mec_rt_plus_oda_reg: MEC 0x24 subtype 0x06, ODA
        #     registration -- declares "RT+ (AID 0x4BD7) lives on
        #     group 11A". Fixed bytes.
        #   * mec_rt_plus_tags: MEC 0x24 subtype 0x16, RT+ tag data --
        #     (CT1=artist, start1=0, len1=artist_len-1) +
        #     (CT2=title, start2=artist_len+3, len2=title_len-1).
        #     Layout verified byte-for-byte against 22 captured songs.
        #   * mec_song_info: MEC 0xAA vendor "song info" -- populates
        #     StereoTool's internal Artist= / Title= / Song= display
        #     fields (harmless if StereoTool's build ignores it).
        # Weather/promo RT updates suppress this whole block on purpose:
        # a text like "Temp: 86F | Feels Like: 90F" contains a " - "
        # style split only if the user's now_playing_format template
        # happens to produce one, and mis-splitting it into a
        # (short-artist, long-title) RT+ tag pair would waste RT+
        # channel time on garbage.
        # Guard artist_len<=32 (see mec_rt_plus_tags docstring) --
        # very long artists fall back to whole-RT single-tag mode.
        if config.use_rt_plus:
            # ODA registration always rides along on every UECP frame so
            # a receiver that catches us mid-broadcast learns "RT+ lives
            # on group 11A with AID 0x4BD7" within one send.
            msg += uecp.mec_rt_plus_oda_reg()
            if artist and title and len(artist) <= 32:
                # Real song: two-tag payload (item.artist + item.title).
                msg += uecp.mec_rt_plus_tags(len(artist), len(title))
                msg += uecp.mec_song_info(f"{artist}{self.RT_PLUS_SEPARATOR}{title}")
            elif rt:
                # Non-song RT (weather / promo / station ID / long-
                # artist fallback). Emit a single tag that covers the
                # whole RT so receivers apply an appropriate content
                # type instead of falling back to the previous song's
                # stale artist/title offsets. Confirmed byte-for-byte
                # against RDS Magic 4's live output on 2026-07-13.
                msg += uecp.mec_rt_plus_tags_generic(len(rt))
        if config.af_frequencies_mhz:
            freqs = [float(f.strip()) for f in config.af_frequencies_mhz.split(",") if f.strip()]
            if freqs:
                msg += uecp.mec_af(freqs)
        # CT is deliberately NOT bundled here. It's sent as its own
        # frame from _tick at the minute boundary, so RDS group 4A is
        # transmitted at :00 seconds -- receivers require that (see
        # _send_ct's docstring for the specific Sangean symptom that
        # forced this split).
        return uecp.build_frame(config.uecp_site_address, config.uecp_encoder_address, self._sqc, msg)

    def _current_ct_fields(self):
        """Fresh UTC time + the server's configured-timezone offset from
        UTC, recomputed on every call (not cached) since this is only
        ever built right before a real send. offset is local-minus-UTC
        in minutes, positive east of UTC -- matches mec_ct()'s own
        expected sign convention (see its docstring)."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            from django.conf import settings
            tz = ZoneInfo(settings.TIME_ZONE)
            offset = now_utc.astimezone(tz).utcoffset()
            offset_minutes = int(offset.total_seconds() // 60) if offset else 0
        except Exception:
            offset_minutes = 0
        return now_utc, offset_minutes

    def _send_ct(self, config):
        """CT-only UECP frame, fired from _tick once per minute at the
        first tick that observes a minute rollover -- so the frame is
        sent within ~1s of :00 seconds and the seconds field encoded
        into MEC 0x0D is a small single-digit value close to 0.

        Why a dedicated frame instead of piggybacking on the PS/RT
        payload: RBDS spec requires group 4A to be transmitted at
        :00 seconds of each minute. Sending CT MEC to StereoTool at
        arbitrary times inside the minute (which is what the previous
        content-payload-bundled approach did -- at the FULL_RESEND
        30s cadence, plus every content change) caused StereoTool to
        emit group 4A at those arbitrary times too. A live Sangean
        receiver refused to decode CT that way; the older NextKast +
        RDS-Magic-4 setup got minute-boundary CT and the same
        receiver decoded fine.

        The connection state (_connected / _sock) is shared with
        _send; CT rides on whatever socket _send already established.
        A CT failure is treated as auxiliary in _tick's catch above:
        _last_error surfaces it but _connected doesn't flip so the
        main content path isn't dragged down by an isolated CT
        write."""
        self._sqc = (self._sqc % 255) + 1
        now_utc, offset_minutes = self._current_ct_fields()
        msg = uecp.mec_ct(now_utc, offset_minutes)
        frame = uecp.build_frame(
            config.uecp_site_address, config.uecp_encoder_address, self._sqc, msg,
        )
        self._transmit(config, frame)

    def _build_ascii_payload(self, config, ps, rt, artist, title):
        # rt is guaranteed by _resolve_rt_content() to be exactly
        # f"{artist}{RT_PLUS_SEPARATOR}{title}" (truncated to 64 chars)
        # whenever artist/title are both non-None, so tag offsets are
        # known exactly -- no substring search needed. If truncation cut
        # into the title, len(title) may overstate what actually
        # survived; an accepted edge-case limitation for very long text,
        # not worth extra complexity for v1.
        rt_plus_tags = None
        if config.use_rt_plus and artist and title:
            artist_start = 0
            title_start = len(artist) + len(self.RT_PLUS_SEPARATOR)
            rt_plus_tags = [
                ascii_protocol.build_rt_plus_tag(ascii_protocol.RT_PLUS_ARTIST, artist_start, len(artist)),
                ascii_protocol.build_rt_plus_tag(ascii_protocol.RT_PLUS_TITLE, title_start, len(title)),
            ]
        commands = ascii_protocol.build_ascii_commands(
            pi_code=config.pi_code, ps=ps, rt=rt, pty=config.pty, music=config.ms,
            di_dynamic_pty=config.di_dynamic_pty, di_compressed=config.di_compressed,
            di_artificial_head=config.di_artificial_head, di_stereo=config.di_stereo,
            rt_plus_tags=rt_plus_tags,
        )
        return ("\n".join(commands) + "\n").encode("utf-8")

    def _transmit(self, config, payload):
        if config.transport == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(payload, (config.host, config.port))
            finally:
                sock.close()
            return

        self._ensure_tcp_connected(config)
        if self._sock is None:
            raise ConnectionError("not connected (TCP reconnect backoff in effect)")
        self._sock.sendall(payload)

    def _ensure_tcp_connected(self, config):
        if self._sock is not None:
            return
        now = time.time()
        backoff = TCP_RECONNECT_BACKOFF[min(self._backoff_index, len(TCP_RECONNECT_BACKOFF) - 1)]
        if now - self._last_connect_attempt < backoff:
            return
        self._last_connect_attempt = now
        try:
            sock = socket.create_connection((config.host, config.port), timeout=5)
            self._sock = sock
            self._backoff_index = 0
        except OSError as exc:
            self._backoff_index = min(self._backoff_index + 1, len(TCP_RECONNECT_BACKOFF) - 1)
            raise ConnectionError(f"TCP connect to {config.host}:{config.port} failed: {exc}") from exc

    def _mark_down(self):
        # Set on the first failure too (not just a connected->down
        # transition) -- otherwise a StereoTool that was never reachable
        # even once since this engine started leaves down_since stuck at
        # None forever, which reads as "unknown" rather than "down since
        # the engine started trying."
        if self._down_since is None:
            self._down_since = time.time()
        self._connected = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _mark_up(self):
        if not self._connected:
            self._connected_since = time.time()
            self._down_since = None
        self._connected = True

    # --- State file ---

    def _write_state(self, config, ps, rt, rt_source, rt_source_name):
        state = {
            "timestamp": time.time(),
            "current_ps": ps,
            "current_rt": rt,
            "rt_source": rt_source,
            "rt_source_name": rt_source_name,
            "protocol": config.protocol,
            "transport": config.transport,
            "host": config.host,
            "port": config.port,
            "connected": self._connected,
            "connected_since": self._connected_since,
            "down_since": self._down_since,
            "last_send_at": self._last_full_resend or None,
            "last_error": self._last_error,
        }
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.rename(STATE_PATH)
