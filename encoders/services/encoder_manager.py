"""Encoder manager — one independent Liquidsoap subprocess per unique
input device, each carrying every enabled Encoder row that shares it.

Originally built on GStreamer's shout2send (mp3/vorbis) plus a separate
ffmpeg subprocess for AAC (shout2send has no AAC support at all — its
sink pad only ever declared MP3/Ogg/WebM caps). Replaced entirely with
Liquidsoap after a live debugging session against a real Shoutcast 2
test server: shout2send's mount-based "http" mode genuinely times out
against this DNAS (it doesn't speak SC2's real wire protocol), and while
its "icy" mode does get past the initial handshake, the actual blocker
turned out to be something else — an empty icy-name header, which SC2's
validator rejects right after accepting the connection (confirmed via
the server's own log: "Bad icy header string [icy-name:]"). Liquidsoap's
`output.shoutcast` has a real, purpose-built `icy_id` parameter for SC2's
multi-stream routing (confirmed via its own log: "Connecting mount sid#4
for @...") and, once `name=` was set, connected and *stayed* connected —
confirmed live against the real test server. Consolidated all 3 formats
(mp3/aac/vorbis) and all 3 protocols onto Liquidsoap for one consistent
backend.

One process per *device*, not per encoder: verified live that two
separate Liquidsoap processes opening the same ALSA capture device
without an explicit subdevice (matching how this project's other ALSA
bridges are addressed — see PROJECT_NOTES.md's subdevice-fragility notes)
silently land on different, unpaired subdevices. One gets the real feed,
the other gets total silence with zero errors or warnings — the exact
same failure shape already seen once with StereoTool. Since Liquidsoap
happily fans one decoded `source` out to many `output.*` calls within a
single script, every Encoder row that shares an input device is bundled
into one shared script/process instead of each getting its own — the
device is only ever opened once.

Liquidsoap's own on_error/reconnect handling already retries transient
connection drops internally per output (confirmed live: "Will try to
reconnect in 3.00 seconds." happened automatically, no code on our side
involved) — so this manager only needs to detect the *whole process*
dying (a real crash, not a transient drop Liquidsoap already recovers
from) and restart it, re-reading the current DB state for that device's
group of encoders rather than reusing whatever was true when it last
started."""
import json
import signal
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import django
django.setup()

from django.db import close_old_connections  # noqa: E402

from encoders.models import Encoder  # noqa: E402

# Paired with the StereoTool HD Output ALSA loopback bridge (second,
# independent Loopback card -- see PROJECT_NOTES.md for the card layout).
# Reads via the `airtap` dsnoop alias (/etc/asound.conf), not a direct
# `plughw:Loopback_1,1,0` open.
#
# History: this WAS a direct plughw open (see the "belt-and-braces"
# framing this comment used to have, with airtap as an unused fallback)
# from when aircheck moved in-process with encoders and there was only
# ever one ALSA reader of this loopback again, making dsnoop's userspace
# ring buffer look like unneeded overhead. Reverted back to `airtap`
# 2026-08-05 after a real production incident: an OS update (most likely
# the `alsa-ucm-conf` bump, NOT the kernel -- both were tried in
# isolation live) changed how `plughw:` negotiates buffer/period geometry
# against `snd_aloop`. Liquidsoap's `input.alsa` started asking for an
# absurd 980 periods and getting back "Alsa error: Input/output error"
# on every single attempt, crash-looping the encoder process indefinitely
# with the station off the air the whole time -- while `arecord` reading
# the exact same raw device worked fine, proving the loopback/StereoTool
# side was never the problem, only Liquidsoap's plughw negotiation was.
# `airtap` pins a fixed period_size=16384/buffer_size=131072 that
# `snd_aloop` accepts regardless of that negotiation, so it's the more
# robust choice going forward even without dsnoop's original multi-
# reader justification -- a few hundred KB of extra kernel ring buffer
# is a trivial cost against another silent, monitoring-invisible dead-air
# incident. If this DEFAULT_INPUT_DEVICE is ever changed again, remember
# `_start_group`'s `host_aircheck = input_device == DEFAULT_INPUT_DEVICE`
# match is a literal string compare -- an Encoder row whose input_device
# doesn't match this constant silently loses aircheck + the telnet
# server, not just its own capture (confirmed hit live during this same
# incident: switching the DB rows to "airtap" without updating this
# constant dropped aircheck from the generated script with no error).
DEFAULT_INPUT_DEVICE = "airtap"

HEALTH_CHECK_SECONDS = 5
RESTART_DELAY_SECONDS = 10
SCRIPT_DIR = Path("/run/isadoraair/liquidsoap")

# Written by library/services/engine.py's _write_now_playing() on every
# track change (station-wide, not per-device -- unlike the per-device
# silence state above, "what's playing" isn't tied to a capture point).
# Watched live by the generated Liquidsoap script via file.watch().
NOW_PLAYING_PATH = "/run/isadoraair/now_playing.json"

# Aircheck-in-liquidsoap: this liquidsoap process hosts a single
# output.file that always writes to AIRCHECK_CURRENT_PATH (a tmpfs
# working file), with reopen wired to telnet so a Start/Stop cycle
# just cuts a fresh file at the same path. aircheck.services.recorder
# is a telnet client that (Start) triggers reopen and remembers the
# session's intended final destination, then (Stop) triggers another
# reopen and moves the just-closed working file to that destination.
#
# Why the fixed path (rather than a runtime-controlled getter)?
# Tried the getter form live 2026-07-20 -- output.file's `.reopen()`
# closes the current file but does NOT re-invoke its filename getter
# on this liquidsoap version; writes silently stop entirely after
# the first reopen. Fixed path + rename dance sidesteps that.
#
# Only ONE liquidsoap group per manager runs telnet+aircheck -- two
# processes binding the same port would clash. See _start_group.
AIRCHECK_TELNET_HOST = "127.0.0.1"
AIRCHECK_TELNET_PORT = 1234
AIRCHECK_CURRENT_PATH = "/run/isadoraair/aircheck-current.audio"
AIRCHECK_OUTPUT_ID = "aircheck"

# shout2send-style protocol mapping doesn't apply here — Liquidsoap has
# distinct operators instead: output.icecast for real Icecast 2 (mount
# path based), output.shoutcast for Shoutcast 1/2 (icy_id based, no real
# "mount" concept — Shoutcast 1 is single-stream so icy_id is left at
# Liquidsoap's own default of 1, Shoutcast 2 uses icy_id parsed from the
# Encoder's `mount` field, e.g. "/4" -> 4, since that's how stream IDs
# were already being entered before this rewrite).


def _liq_string(value):
    """Escape a value for a Liquidsoap string literal."""
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _slug(device):
    """Turn an ALSA device string into something safe for a filename."""
    return "".join(c if c.isalnum() else "_" for c in device)


def _effective_input_device(encoder):
    return encoder.input_device or DEFAULT_INPUT_DEVICE


def _group_by_input_device(encoders):
    groups = defaultdict(list)
    for encoder in encoders:
        groups[_effective_input_device(encoder)].append(encoder)
    return groups


def _format_block(encoder):
    if encoder.format == "mp3":
        # CBR at 192 kbps and up (already transparent -- CBR keeps a
        # predictable buffer fill rate for streaming clients); LAME ABR
        # below that where variable frame allocation buys real audible
        # quality over CBR at the same average bitrate. Both paths sit
        # on Ubuntu's libmp3lame (LAME 3.101) at internal_quality=0,
        # LAME's slowest/highest-quality algorithm -- confirmed to be
        # the %mp3 default by an md5-equal comparison of explicit vs
        # implicit encodes.
        br = encoder.bitrate_kbps
        if br >= 192:
            return f"%mp3(bitrate={br})"
        return f"%mp3.abr(bitrate={br}, internal_quality=0)"
    if encoder.format == "aac":
        # Route AAC through /usr/local/bin/fdkaac (upstream nu774/fdkaac
        # linked against upstream mstorsjo/fdk-aac 2.0.3, both installed
        # under /usr/local from source) rather than ffmpeg's native AAC
        # encoder or Ubuntu's Liquidsoap %fdkaac binding.
        #
        # Why not ffmpeg's native "aac"? LC-only, and sounds bad at low
        # bitrates -- it exists as a fallback so ffmpeg can produce AAC
        # out of the box, not as a serious streaming encoder.
        #
        # Why not Liquidsoap's %fdkaac(...) format? Ubuntu 25.10's
        # liquidsoap package isn't built with the fdkaac OCaml binding
        # (see --list-plugins -- no liquidsoap_fdkaac), so %fdkaac
        # parses at check time but throws "unsupported format" at
        # runtime.
        #
        # Why not Ubuntu's /usr/bin/fdkaac? Ubuntu ships libfdk-aac2
        # with SBR (HE-AAC) and Parametric Stereo (HE-AACv2) stripped
        # for legacy software-patent reasons -- profile=5 and
        # profile=29 both fail with "unsupported profile".
        #
        # Profile chosen from bitrate at the classic Coding Technologies
        # aacPlus crossover points: HE-AACv2 (mono core with stereo
        # synthesized from PS side info) is the standard 24-64 kbps
        # tier; HE-AACv1 (SBR only) is 80-96 kbps; LC takes over at
        # 128+ where SBR's bit-budget advantage stops mattering.
        #
        # fdkaac flags: -R raw input, S16L stereo 44.1k matches the
        # Liquidsoap PCM stream (header=false below); -f 2 emits ADTS
        # framing suitable for streaming; -a 1 keeps afterburner on for
        # extra quality at negligible CPU cost; -S silences per-frame
        # progress writes (which would otherwise flood the encoder log).
        br = encoder.bitrate_kbps
        if br <= 64:
            profile = 29  # HE-AACv2
        elif br <= 96:
            profile = 5   # HE-AAC
        else:
            profile = 2   # LC
        cmd = (
            "/usr/local/bin/fdkaac -R --raw-channels 2 --raw-rate 44100 "
            f"--raw-format S16L -p {profile} -b {br * 1000} -f 2 -a 1 -S "
            "-o - -"
        )
        # %external doesn't accept mime/extension params here; the MIME
        # ("audio/aacp" -- aacPlus convention for HE-AAC, LC muxes fine
        # under it in ADTS too) is applied downstream on
        # output.shoutcast/output.icecast via their `format=` argument
        # (see _output_block).
        return f"%external(process={_liq_string(cmd)}, header=false)"
    return f"%vorbis(bitrate={encoder.bitrate_kbps})"  # vorbis


def _output_block(encoder, source_var):
    fmt = _format_block(encoder)
    # AAC is emitted via %external(fdkaac) which can't advertise its
    # own MIME type, so the ICY/HTTP content-type has to come from the
    # output.shoutcast/icecast call. mp3 and vorbis auto-advertise from
    # their format symbol, so an explicit format= there would just be
    # redundant.
    # For AAC via %external, we also have to explicitly opt into
    # in-band ICY title metadata -- Liquidsoap auto-picks it for
    # built-in encoders (yes for mp3, no for ogg) but can't guess for
    # an opaque external process. Shoutcast supports ICY metadata over
    # AAC/ADTS streams, so we want it on.
    if encoder.format == "aac":
        format_arg = ', format="audio/aacp", send_icy_metadata=true'
    else:
        format_arg = ""
    common = (
        f"host={_liq_string(encoder.host)}, port={encoder.port}, "
        f"password={_liq_string(encoder.password)}, "
        f"name={_liq_string(encoder.station_name)}, "
        f"genre={_liq_string(encoder.genre)}, "
        f"url={_liq_string(encoder.url)}, "
        f"public={'true' if encoder.public else 'false'}"
        f"{format_arg}"
    )
    if encoder.protocol == "icecast":
        return (
            f"output.icecast({fmt}, {common}, "
            f"mount={_liq_string(encoder.mount)}, "
            f"user={_liq_string(encoder.username)}, {source_var})"
        )
    if encoder.protocol == "shoutcast2":
        stream_id = encoder.mount.strip("/") or "1"
        return f"output.shoutcast({fmt}, {common}, icy_id={stream_id}, {source_var})"
    return f"output.shoutcast({fmt}, {common}, {source_var})"  # shoutcast1


def _aircheck_format_block(cfg):
    """Format for the aircheck output.file, driven by AircheckConfig.
    Format changes require an encoder-manager restart to take effect
    (the format bakes into the script at build time). Bitrate changes
    also require a restart. Path/directory/filename_template changes do
    NOT require a restart -- those are resolved at Start button press
    time by aircheck.services.recorder from the config's current values.

    Kept parallel to _format_block (encoder streaming) rather than
    factored together on purpose: aircheck may want lossless (flac/wav)
    options that make no sense for a streaming encoder, and streaming
    may want CBR/ABR quality knobs that aren't meaningful for archival."""
    fmt = cfg.audio_format
    br = cfg.effective_bitrate() or "64k"
    br_k = int(str(br).lower().rstrip("k")) if str(br).lower().endswith("k") else int(br) // 1000
    if fmt == "he_aac":
        # fdkaac -f 2 emits ADTS-framed AAC to stdout (streamable), not
        # -f 5 m4a (which requires seekable output to backfill the moov
        # atom on close and thus can't be piped via %external). The
        # aircheck recorder's stop path finalizes he_aac sessions with
        # `ffmpeg -c copy adts.aac -> session.m4a` -- a container swap,
        # no re-encode. Profile tiers match the streaming Encoder path:
        # -p 29 = HE-AACv2 up through 64k, -p 5 = HE-AACv1 at 80-96k,
        # -p 2 = LC at 128k+. -a 1 = afterburner on for extra quality.
        if br_k <= 64:
            profile = 29
        elif br_k <= 96:
            profile = 5
        else:
            profile = 2
        cmd = (
            "/usr/local/bin/fdkaac -R --raw-channels 2 --raw-rate 44100 "
            f"--raw-format S16L -p {profile} -b {br_k * 1000} -f 2 -a 1 -S -o - -"
        )
        return f"%external(process={_liq_string(cmd)}, header=false)"
    if fmt == "mp3":
        if br_k >= 192:
            return f"%mp3(bitrate={br_k})"
        return f"%mp3.abr(bitrate={br_k}, internal_quality=0)"
    if fmt == "flac":
        return "%flac"
    if fmt == "wav":
        return "%wav"
    return "%mp3(bitrate=192)"  # defensive fallback


def _aircheck_block():
    """Liquidsoap fragment: single output.file that always writes the
    live air source to AIRCHECK_CURRENT_PATH (on tmpfs). Session Start
    calls `aircheck.reopen` over telnet -- that closes the current
    working file and opens a fresh one at the same path. Session Stop
    calls reopen again and then aircheck.services.recorder moves the
    working file to the session's final destination.

    Design note: we ALWAYS encode, even when idle. On this box the mp3
    pipe is ~1% of one core so this is a fine trade for zero-glitch
    session-start (no encoder spin-up latency).

    reopen_delay defaults to 120s in liquidsoap, which would silently
    swallow a Stop-then-quick-Start. We drop it to 0 so back-to-back
    session toggles work.

    flush=true forces a write-through on every encoded chunk so the
    Stop-then-move sequence sees a complete file instead of a
    still-buffered tail."""
    from aircheck.models import AircheckConfig  # lazy to avoid import cycle at module load
    cfg = AircheckConfig.load()
    fmt_block = _aircheck_format_block(cfg)
    return [
        '',
        '# --- Aircheck output.file (Start/Stop cut files via telnet reopen) ---',
        f'aircheck_output = output.file(',
        f'  id={_liq_string(AIRCHECK_OUTPUT_ID)},',
        f'  fallible=true,',
        f'  flush=true,',
        f'  reopen_delay={{0.}},',
        f'  {fmt_block},',
        f'  {_liq_string(AIRCHECK_CURRENT_PATH)},',
        f'  source',
        f')',
        # server.register exposes the output's .reopen() method as a
        # telnet command. Callback signature is (string) -> string;
        # arg is ignored, return value is echoed to the caller.
        'def aircheck_reopen_handler(_) =',
        '  aircheck_output.reopen()',
        '  "reopened"',
        'end',
        f'server.register(namespace={_liq_string(AIRCHECK_OUTPUT_ID)}, "reopen", aircheck_reopen_handler)',
    ]


def _telnet_server_block():
    """Liquidsoap fragment enabling the local telnet control server so
    aircheck.services.recorder can toggle aircheck_path + call
    aircheck.reopen without an encoder restart. Bound to localhost
    only -- no external exposure. Only injected on the main-air group
    (see EncoderManager._start_group) because two liquidsoap processes
    binding the same port would clash."""
    return [
        f'settings.server.telnet.set(true)',
        f'settings.server.telnet.bind_addr.set({_liq_string(AIRCHECK_TELNET_HOST)})',
        f'settings.server.telnet.port.set({AIRCHECK_TELNET_PORT})',
        '',
    ]


def build_liquidsoap_script(input_device, encoders, host_aircheck=False):
    """One shared `input.alsa` (the device is only ever opened once) fanned
    out to one output.* block per encoder that uses this device.

    Also wraps the shared source in `blank.detect` and self-reports
    silence to a small JSON state file that monitoring/services/probes.py
    reads (see MonitorCheck kind="audio_silence") -- this is deliberately
    NOT a second ALSA capture process. Two independent readers on the
    same plughw device+subdevice either fail to open or silently land on
    different, unpaired subdevices (the exact failure mode already hit
    once with StereoTool and again with the original per-encoder
    Liquidsoap design, see this file's own docstring) -- self-reporting
    from inside the process that already holds the device sidesteps that
    entirely. Verified against this box's real installed Liquidsoap
    2.4.0+dev (`liquidsoap --list-functions-md` + a standalone --check
    and a live short-lived run) before wiring this in live."""
    slug = _slug(input_device)
    state_path = f"/run/isadoraair/liquidsoap_silence_{slug}.json"
    lines = []
    if host_aircheck:
        # Telnet server must be configured BEFORE any source/output
        # definitions (liquidsoap parses `settings.*` at script-eval
        # time, and some settings are read-only once sources exist).
        lines += _telnet_server_block()
    lines += [
        # buffer_size=1.0s: input.alsa defaults to `null` (= one frame
        # duration = ~20ms = 882 samples), which the raw plughw driver
        # honors literally -- with that little headroom any scheduler
        # jitter (a competing icecast reconnect burst, an fdkaac cache
        # miss, GC) causes an "Overrun! ... trying to recover" and a
        # 100-200ms glitch on the stream. dsnoop used to silently
        # upgrade to 16384 samples on our behalf, which is why the
        # airtap variant never showed this. 1.0s = comfortable
        # headroom on a broadcast pipeline that's already
        # downstream-latency-dominated.
        f'source = input.alsa(buffer_size=1.0, device={_liq_string(input_device)})',
        'source = blank.detect(threshold=-40.0, max_blank=20.0, min_noise=0.5, source)',
        '',
        # `last_blank` mirrors the most recent transition state so the 60s
        # heartbeat below re-writes with the *correct* is_blank -- otherwise
        # a heartbeat during a real silence would overwrite it with `false`
        # and mask the outage from the dashboard.
        #
        # `since_ts` is the wall-clock time of the most recent is_blank
        # transition (or script start), NOT the time of the last write.
        # The dashboard's "Stable for X hrs" caption is computed against
        # this so the heartbeat rewriting the file every 60s doesn't
        # reset the visible counter -- only a real silence<->noise flip
        # does. `write_silence_state` therefore takes the transition
        # timestamp as a parameter: the on_blank/on_noise/startup call
        # sites pass `time()` to bump it, and the heartbeat call site
        # passes `since_ts()` to carry the current one forward unchanged.
        'last_blank = ref(false)',
        'since_ts = ref(time())',
        'def write_silence_state(is_blank, since) =',
        '  last_blank.set(is_blank)',
        '  since_ts.set(since)',
        f'  state = json.stringify(compact=true, {{is_blank = is_blank, timestamp = time(), since = since}})',
        # temp_dir must live on the same filesystem as the target for the
        # atomic rename to succeed. Without this, liquidsoap defaults temp
        # to /tmp and logs "Atomic rename failed!" on every write (harmless
        # -- the write still happens non-atomically -- but with the 60s
        # heartbeat it turns into once-a-minute log spam).
        f'  file.write(data=state, atomic=true, temp_dir="/run/isadoraair", {_liq_string(state_path)})',
        'end',
        '',
        'source.on_blank(synchronous=false, {write_silence_state(true, time())})',
        'source.on_noise(synchronous=false, {write_silence_state(false, time())})',
        # on_blank/on_noise only fire on a transition -- without this,
        # a feed that's been continuously fine since startup would never
        # write a state file at all, and the dashboard would show
        # "unknown" forever instead of "ok". start_blank defaults to
        # false, so the matching initial assumption here is "not blank".
        'write_silence_state(false, time())',
        # Periodic heartbeat: re-touch the state file every 60s carrying
        # the last known is_blank AND the last known transition timestamp
        # (so the dashboard's stable-for counter keeps accumulating
        # across heartbeats -- only a real transition resets it). Lets
        # probe_audio_silence use a tight staleness bound as a genuine
        # "liquidsoap wedged/dead" signal without falsely tripping on a
        # stream that's been continuously fine (the original 24h bound
        # was guaranteed to hit "unknown" once per day on a perfectly
        # healthy feed -- caught live).
        'thread.run(every=60., {write_silence_state(last_blank(), since_ts())})',
        '',
        # Wrapped in try/catch: now_playing.json is written in-place (not
        # atomic rename -- see engine.py's _write_now_playing for why),
        # so file.watch's callback can race a write and read a
        # truncated/incomplete file. An uncaught error here (real, hit in
        # production -- a "Parse error" mid-write) kills this callback
        # permanently: file.watch never fires again afterward, silently
        # freezing the stream's metadata at whatever last parsed
        # correctly until the encoder is restarted. Catching and skipping
        # just that one invocation means the NEXT write (a moment later,
        # once the file is fully written) updates normally instead.
        'def update_now_playing() =',
        '  try',
        f'    content = file.contents({_liq_string(NOW_PLAYING_PATH)})',
        '    parsed = metadata.json.parse(content)',
        '    source.insert_metadata(new_track=true, parsed)',
        '  catch err do',
        '    print("update_now_playing: skipping malformed read (#{err})")',
        '  end',
        'end',
        f'file.watch({_liq_string(NOW_PLAYING_PATH)}, update_now_playing)',
        # file.watch only fires on *changes* -- without this initial call,
        # a freshly (re)started encoder would carry no metadata until the
        # next real track change (same "prime it once at startup" pattern
        # as write_silence_state(false) above).
        'update_now_playing()',
        '',
    ]
    lines += [_output_block(encoder, "source") for encoder in encoders]
    if host_aircheck:
        lines += _aircheck_block()
    return "\n".join(lines) + "\n"


class EncoderManager:
    def __init__(self):
        self.running = False
        self._procs = {}  # input_device -> subprocess.Popen
        self._scripts = {}  # input_device -> Path
        self._restart_at = {}  # input_device -> earliest restart timestamp

    def start(self):
        self.running = True
        close_old_connections()
        SCRIPT_DIR.mkdir(parents=True, exist_ok=True)

        groups = _group_by_input_device(Encoder.objects.filter(enabled=True))
        for input_device, encoders in groups.items():
            self._start_group(input_device, encoders)

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        active_encoders = sum(len(v) for v in groups.values())
        print(f"Encoders started ({len(self._procs)} process(es), {active_encoders} stream(s)).")
        while self.running:
            time.sleep(HEALTH_CHECK_SECONDS)
            self._check_health()
        self.stop()

    def stop(self):
        self.running = False
        for proc in self._procs.values():
            proc.terminate()
        for proc in self._procs.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for script_path in self._scripts.values():
            script_path.unlink(missing_ok=True)
        print("Encoders stopped.")

    def _handle_signal(self, signum, frame):
        print("\nShutting down...")
        self.running = False

    def _start_group(self, input_device, encoders):
        # file.watch() in the generated script throws an uncaught runtime
        # error if this file doesn't exist yet at the moment Liquidsoap
        # calls it -- guarantee it exists (placeholder is fine) before the
        # script is ever launched, not just on first-ever manager start.
        if not Path(NOW_PLAYING_PATH).is_file():
            # timestamp as a string, not a float -- see engine.py's
            # _write_now_playing() for why (metadata.json.parse() requires
            # a uniformly string-valued object).
            Path(NOW_PLAYING_PATH).write_text(
                json.dumps({"title": "", "artist": "", "timestamp": str(time.time())}),
                encoding="utf-8",
            )

        # Attach the aircheck output.file + telnet server only to the
        # main-air group (the one whose input_device matches
        # DEFAULT_INPUT_DEVICE). Rationale: two liquidsoap processes
        # can't bind the same telnet port, and aircheck records what
        # goes to air -- that's the DEFAULT_INPUT_DEVICE tap.
        host_aircheck = input_device == DEFAULT_INPUT_DEVICE
        script = build_liquidsoap_script(input_device, encoders, host_aircheck=host_aircheck)
        script_path = SCRIPT_DIR / f"encoders_{_slug(input_device)}.liq"
        script_path.write_text(script, encoding="utf-8")
        try:
            proc = subprocess.Popen(["liquidsoap", str(script_path)])
        except Exception as exc:
            print(f"  [{input_device}] Failed to start: {exc}")
            return
        self._procs[input_device] = proc
        self._scripts[input_device] = script_path
        names = ", ".join(e.name for e in encoders)
        print(f"  [{input_device}] Started (Liquidsoap) -> {names}")

    def _check_health(self):
        now = time.monotonic()

        # Dead processes: schedule a restart time instead of blocking here —
        # with multiple device groups, a blocking sleep would serialize
        # their restarts one-by-one instead of handling each independently.
        for input_device, proc in list(self._procs.items()):
            if proc.poll() is not None:
                print(f"  [{input_device}] Liquidsoap exited (code {proc.returncode}), restarting in {RESTART_DELAY_SECONDS}s")
                del self._procs[input_device]
                script_path = self._scripts.pop(input_device, None)
                if script_path:
                    script_path.unlink(missing_ok=True)
                self._restart_at[input_device] = now + RESTART_DELAY_SECONDS

        # Device groups whose restart delay has elapsed — re-read the
        # current DB state rather than reusing the encoder list from
        # whenever this group last started, in case rows changed since.
        for input_device, at in list(self._restart_at.items()):
            if now < at:
                continue
            del self._restart_at[input_device]
            close_old_connections()
            groups = _group_by_input_device(Encoder.objects.filter(enabled=True))
            encoders = groups.get(input_device)
            if not encoders:
                print(f"  [{input_device}] No enabled encoders left for this device, not restarting.")
                continue
            print(f"  [{input_device}] Restarting...")
            self._start_group(input_device, encoders)
