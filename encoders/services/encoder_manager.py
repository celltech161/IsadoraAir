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
import uuid
from collections import defaultdict
from pathlib import Path

import django
django.setup()

from django.db import close_old_connections  # noqa: E402

from encoders.models import Encoder  # noqa: E402
from monitoring.models import emit_event  # noqa: E402

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
SCRIPT_DIR = Path("/run/isadoraair/liquidsoap")
STATE_DIR = Path("/run/isadoraair")

# Reliability hardening (2026-08-05, post-outage): a flat 10s retry delay
# treated "just failed" and "has been crash-looping for an hour" the
# same way -- fine for a genuine one-off blip, indistinguishable from a
# real config/hardware problem that will never self-heal and just
# deserves cheaper, less frequent retries. Capped exponential backoff,
# independent per input-device group (see EncoderManager._retry_index).
# 5/10/30/60/300s -- first few steps stay close to the old flat 10s so a
# transient blip still recovers about as fast as before; the 300s cap
# keeps a genuinely wedged group from hammering ALSA/the network forever.
RETRY_BACKOFF_SECONDS = [5, 10, 30, 60, 300]

# How long a group's CURRENT generation must show real audio_observed
# (via the Liquidsoap-written audio-state file, see build_liquidsoap_script)
# before backoff resets back to the front of RETRY_BACKOFF_SECONDS.
# Same clock monitoring/services/probes.py's own stabilization gate
# uses -- one "how long is long enough to trust this" concept shared by
# both the supervisor's own backoff-reset decision and the dashboard's
# ok-vs-still-stabilizing decision, not two independently-tuned ones
# that could disagree with each other.
STABILIZATION_SECONDS = 25

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
    """Turn an ALSA device string into something safe for a filename.

    Collision note (Phase 8 hardening review): two DIFFERENT device
    strings could in principle slug to the same string (e.g. two
    strings differing only in which punctuation character appears at
    a given position). Assessed against this project's real device
    naming scheme -- "airtap" and "plughw:CARD,DEV,SUBDEV" -- and
    accepted as a live, low-probability risk rather than "fixed" by
    changing the filename scheme: doing so would require migrating
    every already-admin-configured MonitorCheck.silence_device_slug /
    encoder_group_slug value at the same time, which is a real
    correctness risk of its own (a stale admin-entered slug silently
    pointing at the wrong file) for a collision that would need two
    genuinely different real ALSA device strings this project has
    never actually used. Revisit if a future input_device naming
    scheme makes a real collision plausible."""
    return "".join(c if c.isalnum() else "_" for c in device)


def _audio_state_path_for_slug(slug):
    """Same file build_liquidsoap_script's write_state() writes and
    probe_audio_silence/probe_encoder_group (monitoring/services/
    probes.py) read -- takes an ALREADY-SLUGGED string. MonitorCheck's
    silence_device_slug/encoder_group_slug fields both store the
    pre-slugged form (an admin-entered config value, matching
    silence_device_slug's own long-established convention), so the
    probes call this variant directly rather than re-deriving a slug
    from a raw device string they were never given."""
    return STATE_DIR / f"liquidsoap_silence_{slug}.json"


def _audio_state_path(input_device):
    """Same file as above, from a raw ALSA device string -- used by
    the manager itself, which always has the real device string on
    hand, never just its slug."""
    return _audio_state_path_for_slug(_slug(input_device))


def _group_state_path_for_slug(slug):
    """Manager-owned per-group process-supervision state file -- see
    EncoderManager._write_group_state and probe_encoder_group. Same
    already-slugged-string convention as _audio_state_path_for_slug."""
    return STATE_DIR / f"encoder_group_{slug}.json"


def _group_state_path(input_device):
    """Same file as above, from a raw ALSA device string."""
    return _group_state_path_for_slug(_slug(input_device))


def _atomic_write_json(path, data):
    """Write-tmp-then-rename -- same idiom already used by
    monitoring/services/monitor.py's _write_state/_write_listener_state.
    No reader (a monitoring poll landing mid-write) can ever observe a
    partially-written or truncated state file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.rename(path)


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
        return f"output.shoutcast({fmt}, {common}, icy_id={encoder.shoutcast_sid}, {source_var})"
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


def build_liquidsoap_script(input_device, encoders, host_aircheck=False, generation=""):
    """One shared `input.alsa` (the device is only ever opened once) fanned
    out to one output.* block per encoder that uses this device.

    Also wraps the shared source in `blank.detect` and self-reports its
    OWN status (not just silence) to a small JSON state file that
    monitoring/services/probes.py reads (MonitorCheck kinds
    "audio_silence" and "encoder_group") -- this is deliberately NOT a
    second ALSA capture process. Two independent readers on the same
    plughw device+subdevice either fail to open or silently land on
    different, unpaired subdevices (the exact failure mode already hit
    once with StereoTool and again with the original per-encoder
    Liquidsoap design, see this file's own docstring) -- self-reporting
    from inside the process that already holds the device sidesteps that
    entirely. Verified against this box's real installed Liquidsoap
    2.4.0+dev (`liquidsoap --list-functions-md` + a standalone --check
    and a live short-lived run) before wiring this in live.

    `generation` is a short opaque id EncoderManager assigns to this
    specific launch (see _start_group) and bakes in here as a literal --
    it's how monitoring cross-checks this file against
    EncoderManager's OWN state file (encoder_group_<slug>.json) to tell
    "the CURRENT child's real status" apart from "a stale file an
    earlier, already-dead child left behind" (2026-08-05 hardening,
    Phase 8). `process.pid()` (confirmed present via
    `liquidsoap --list-functions-md` -- type `() -> int`) is Liquidsoap
    reporting its OWN real pid at runtime, not a value the manager has
    to guess or pass in before the process exists.

    Startup status is explicitly "starting", never an optimistic
    healthy default -- the ROOT CAUSE of the 2026-08-05 false-green
    outage was the previous version of this function calling
    write_silence_state(false, ...) (= "not blank" = "fine")
    unconditionally at script start, before any audio was ever
    verified. A crash-looping child re-asserted that "fine" every
    15-20s, always beating monitoring's staleness window. is_blank is
    now a real three-state value (null while starting, then true/false
    once blank.detect has actually observed something) rather than a
    boolean that could only ever lie in the direction of "healthy" when
    unset."""
    audio_state_path = str(_audio_state_path(input_device))
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
        f'generation = {_liq_string(generation)}',
        f'input_device_str = {_liq_string(input_device)}',
        # Liquidsoap's own wall-clock time as of the first line actually
        # executing -- more accurate than a Python-side pre-Popen()
        # timestamp, which would be measured slightly before this
        # process even exec'd.
        'started_at = time()',
        '',
        # `last_status`/`last_is_blank` mirror the most recent transition
        # so the 60s heartbeat below re-writes with the *correct*
        # values -- otherwise a heartbeat during a real silence would
        # overwrite it with a healthy-looking state and mask the outage
        # from the dashboard.
        #
        # `since_ts` is the wall-clock time of the most recent status
        # transition (or script start), NOT the time of the last write.
        # The dashboard's "Stable for X hrs" caption is computed against
        # this so the heartbeat rewriting the file every 60s doesn't
        # reset the visible counter -- only a real transition does.
        # `write_state` therefore takes the transition timestamp as a
        # parameter: the on_blank/on_noise/startup call sites pass
        # `time()` to bump it, the heartbeat call site passes
        # `since_ts()` to carry the current one forward unchanged.
        'last_status = ref("starting")',
        'last_is_blank = ref(null)',
        'audio_observed = ref(false)',
        'since_ts = ref(time())',
        'def write_state(status, is_blank, since) =',
        '  last_status.set(status)',
        '  last_is_blank.set(is_blank)',
        '  since_ts.set(since)',
        '  state = json.stringify(compact=true, {',
        '    status = status,',
        '    is_blank = is_blank,',
        '    audio_observed = audio_observed(),',
        '    input_device = input_device_str,',
        '    pid = process.pid(),',
        '    generation = generation,',
        '    started_at = started_at,',
        '    since = since,',
        '    timestamp = time(),',
        '  })',
        # temp_dir must live on the same filesystem as the target for the
        # atomic rename to succeed. Without this, liquidsoap defaults temp
        # to /tmp and logs "Atomic rename failed!" on every write (harmless
        # -- the write still happens non-atomically -- but with the 60s
        # heartbeat it turns into once-a-minute log spam).
        f'  file.write(data=state, atomic=true, temp_dir="/run/isadoraair", {_liq_string(audio_state_path)})',
        'end',
        '',
        # is_blank=null on the transition writes below would be WRONG --
        # on_blank/on_noise firing means blank.detect has definitively
        # observed something real, so null (meaning "not yet verified")
        # never belongs on either of these two lines, only on the
        # startup line further down.
        'source.on_blank(synchronous=false, {write_state("silent", true, time())})',
        'source.on_noise(synchronous=false, {',
        '  audio_observed.set(true)',
        '  write_state("audio_ok", false, time())',
        '})',
        # Startup write -- deliberately "starting"/null, NEVER a healthy
        # default (see this function's own docstring on why the old
        # unconditional "false" default here was the actual root cause
        # of a real production outage going undetected). A process that
        # crashes before on_noise ever fires leaves this exact
        # "starting"/null record as the last thing it ever wrote --
        # correctly unresolved, not a false all-clear.
        'write_state("starting", null, time())',
        # Periodic heartbeat: re-touch the state file every 60s carrying
        # the last known status/is_blank AND the last known transition
        # timestamp (so the dashboard's stable-for counter keeps
        # accumulating across heartbeats -- only a real transition
        # resets it). Lets the monitoring probes use a tight staleness
        # bound as a genuine "liquidsoap wedged/dead" signal without
        # falsely tripping on a stream that's been continuously fine
        # (an earlier, much longer bound was guaranteed to hit "unknown"
        # periodically even on a perfectly healthy feed -- caught live).
        'thread.run(every=60., {write_state(last_status(), last_is_blank(), since_ts())})',
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
        self._procs = {}          # input_device -> subprocess.Popen
        self._scripts = {}        # input_device -> Path
        self._current = {}        # input_device -> {"generation":.., "pid":.., "launched_at":..}, this group's live child
        self._retry_index = {}    # input_device -> index into RETRY_BACKOFF_SECONDS (independent per group)
        self._retry_at = {}       # input_device -> time.monotonic() of next retry attempt
        self._stabilized = {}     # input_device -> bool, has the CURRENT generation already earned a backoff reset
        self._meta = {}           # input_device -> dict of supervision fields that persist across relaunches
        # (consecutive_failures, last_failure_message, last_successful_start, last_exit_at, last_exit_code)
        self._last_log = {}       # input_device -> (message, monotonic_time), for light stdout throttling

    def start(self):
        self.running = True
        close_old_connections()
        SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        groups = _group_by_input_device(Encoder.objects.filter(enabled=True))
        for input_device, encoders in groups.items():
            if not self._start_group(input_device, encoders):
                self._schedule_retry(input_device)

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
        # Phase 8: an intentional shutdown must not leave either state
        # file looking like a still-supervised, possibly-healthy group.
        # Mark the audio-state file "dead" (in case anything's still
        # reading it) AND remove the group-state file outright -- "no
        # group-state file" is itself an unambiguous "not supervised"
        # signal, not to be confused with corruption/staleness.
        for input_device, script_path in list(self._scripts.items()):
            script_path.unlink(missing_ok=True)
            self._mark_audio_state_dead(input_device)
            try:
                _group_state_path(input_device).unlink(missing_ok=True)
            except OSError:
                pass
        print("Encoders stopped.")

    def _handle_signal(self, signum, frame):
        print("\nShutting down...")
        self.running = False

    # ------------------------------------------------------------------
    # Per-group bookkeeping helpers
    # ------------------------------------------------------------------

    def _group_meta(self, input_device):
        return self._meta.setdefault(input_device, {
            "consecutive_failures": 0, "last_failure_message": "",
            "last_successful_start": None, "last_exit_at": None, "last_exit_code": None,
        })

    def _log(self, input_device, message, force=False):
        """Print to stdout/journal, throttled: an IDENTICAL message for
        the same group within 30s is suppressed (Phase 7 req #7 --
        rate-limit repeated identical errors in the journal) unless
        `force`. The dashboard-facing record (SystemEvent, via
        emit_event) is separately and correctly coalesced by its own
        existing 60s window regardless of this throttle -- this only
        controls the raw, unbounded-length journal stream."""
        now = time.monotonic()
        last_msg, last_at = self._last_log.get(input_device, (None, 0.0))
        if not force and message == last_msg and (now - last_at) < 30:
            return
        self._last_log[input_device] = (message, now)
        print(f"  [{input_device}] {message}")

    def _read_audio_state(self, input_device):
        path = _audio_state_path(input_device)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _mark_audio_state_dead(self, input_device):
        """Best-effort -- called on both an observed child exit and an
        intentional manager stop(). Overwrites the Liquidsoap-owned
        audio-state file so no reader can see stale "healthy" content
        once this specific process is confirmed gone. Preserves
        whatever generation/pid it last held (informational -- "this
        is what died"), only flips status/is_blank."""
        try:
            data = self._read_audio_state(input_device)
            data.update({
                "status": "dead", "is_blank": None,
                "input_device": input_device, "timestamp": time.time(),
            })
            _atomic_write_json(_audio_state_path(input_device), data)
        except OSError:
            pass

    def _write_group_state(self, input_device, next_retry_at=None):
        """Full snapshot write of the manager-owned per-group state
        file -- see _group_state_path. Always derives pid/generation/
        launched_at from self._current (empty dict if this group has no
        live child right now) rather than accepting them as parameters,
        so every call site can't accidentally write mismatched values."""
        meta = self._group_meta(input_device)
        current = self._current.get(input_device, {})
        state = {
            "input_device": input_device,
            "pid": current.get("pid"),
            "generation": current.get("generation"),
            "launched_at": current.get("launched_at"),
            "last_successful_start": meta["last_successful_start"],
            "last_exit_at": meta["last_exit_at"],
            "last_exit_code": meta["last_exit_code"],
            "consecutive_failures": meta["consecutive_failures"],
            "last_failure_message": meta["last_failure_message"],
            "next_retry_at": next_retry_at,
            "timestamp": time.time(),
        }
        try:
            _atomic_write_json(_group_state_path(input_device), state)
        except OSError as exc:
            print(f"  [{input_device}] Failed to write group state: {exc}")

    def _schedule_retry(self, input_device):
        """Advance (never reset except by _check_health's stabilization
        check) this group's backoff index and schedule its next retry,
        capped at RETRY_BACKOFF_SECONDS[-1]. Independent per group
        (Phase 7 req #3/#12) -- a dict keyed by input_device, no shared
        counter that could let one group's failures throttle another's
        retries. Also refreshes the group-state file's next_retry_at
        immediately, rather than leaving it stale until the next tick."""
        index = self._retry_index.get(input_device, 0)
        delay = RETRY_BACKOFF_SECONDS[min(index, len(RETRY_BACKOFF_SECONDS) - 1)]
        self._retry_index[input_device] = index + 1
        self._retry_at[input_device] = time.monotonic() + delay
        self._write_group_state(input_device, next_retry_at=time.time() + delay)
        return delay

    # ------------------------------------------------------------------
    # Launch / supervision
    # ------------------------------------------------------------------

    def _start_group(self, input_device, encoders):
        """Launch one Liquidsoap child for `input_device`. Returns True
        on a successful Popen(), False on failure -- EVERY call site is
        responsible for calling _schedule_retry(input_device) when this
        returns False (Phase 7 req #1: a failed initial launch must
        enter the same retry schedule a later child-exit would, not
        just print and silently give up on the group forever)."""
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

        meta = self._group_meta(input_device)
        generation = uuid.uuid4().hex[:12]
        # Attach the aircheck output.file + telnet server only to the
        # main-air group (the one whose input_device matches
        # DEFAULT_INPUT_DEVICE). Rationale: two liquidsoap processes
        # can't bind the same telnet port, and aircheck records what
        # goes to air -- that's the DEFAULT_INPUT_DEVICE tap.
        host_aircheck = input_device == DEFAULT_INPUT_DEVICE
        script = build_liquidsoap_script(input_device, encoders, host_aircheck=host_aircheck, generation=generation)
        script_path = SCRIPT_DIR / f"encoders_{_slug(input_device)}.liq"
        script_path.write_text(script, encoding="utf-8")

        launched_at = time.time()
        try:
            proc = subprocess.Popen(["liquidsoap", str(script_path)])
        except Exception as exc:
            meta["consecutive_failures"] += 1
            meta["last_failure_message"] = f"Popen failed: {exc}"
            meta["last_exit_at"] = launched_at
            meta["last_exit_code"] = None
            self._current.pop(input_device, None)
            self._log(input_device, f"Failed to start: {exc} (failure #{meta['consecutive_failures']} in a row)", force=True)
            emit_event(
                category="encoder", level="error",
                title=f"Encoder group '{input_device}' failed to launch",
                detail={"input_device": input_device, "error": str(exc), "consecutive_failures": meta["consecutive_failures"]},
                dedupe_key=f"encoder|launch-failed|{input_device}",
            )
            self._write_group_state(input_device)
            return False

        # Invalidate any stale audio-state file a PREVIOUS generation
        # left behind BEFORE returning control to the health-check loop
        # -- closes the real window between this Popen() call returning
        # and the script's own first line (which also writes "starting")
        # actually executing. A monitoring poll landing in that gap
        # must never see an earlier generation's possibly-"audio_ok"
        # content (Phase 8 req #2/#7).
        _atomic_write_json(_audio_state_path(input_device), {
            "status": "starting", "is_blank": None, "audio_observed": False,
            "input_device": input_device, "pid": proc.pid, "generation": generation,
            "started_at": launched_at, "since": launched_at, "timestamp": launched_at,
        })

        self._procs[input_device] = proc
        self._scripts[input_device] = script_path
        self._current[input_device] = {"generation": generation, "pid": proc.pid, "launched_at": launched_at}
        self._stabilized[input_device] = False
        self._write_group_state(input_device)
        names = ", ".join(e.name for e in encoders)
        self._log(input_device, f"Started (Liquidsoap pid={proc.pid}, generation={generation}) -> {names}", force=True)
        return True

    def _handle_exit(self, input_device, returncode):
        self._procs.pop(input_device, None)
        script_path = self._scripts.pop(input_device, None)
        if script_path:
            script_path.unlink(missing_ok=True)
        self._stabilized.pop(input_device, None)

        meta = self._group_meta(input_device)
        meta["consecutive_failures"] += 1
        meta["last_exit_at"] = time.time()
        meta["last_exit_code"] = returncode
        meta["last_failure_message"] = f"Liquidsoap exited with code {returncode}"
        self._current.pop(input_device, None)  # no live child -- Phase 3 req #4: mark dead immediately

        self._mark_audio_state_dead(input_device)

        delay = self._schedule_retry(input_device)
        self._log(
            input_device,
            f"Liquidsoap exited (code {returncode}), failure #{meta['consecutive_failures']} in a row, retrying in {delay}s",
        )
        # Crash-looping (3+ in a row) escalates to "error" -- a single
        # exit could be a one-off blip, a genuine loop deserves more
        # attention than the dashboard's own aggregate check alone.
        emit_event(
            category="encoder",
            level="error" if meta["consecutive_failures"] >= 3 else "warning",
            title=f"Encoder group '{input_device}' Liquidsoap exited",
            detail={
                "input_device": input_device, "exit_code": returncode,
                "consecutive_failures": meta["consecutive_failures"], "retry_in_seconds": delay,
            },
            dedupe_key=f"encoder|child-exit|{input_device}",
        )

    def _check_stabilization(self, input_device):
        """Has the group's CURRENT generation shown real, continuous
        audio for at least STABILIZATION_SECONDS? If so, reset its
        backoff to the front of the schedule and record the win.
        Phase 7 req #5/#6: a successful Popen() alone must never do
        this -- only sustained, self-reported real health earns it."""
        if self._stabilized.get(input_device):
            return
        current = self._current.get(input_device)
        if not current:
            return
        audio_state = self._read_audio_state(input_device)
        # Generation must match THIS specific launch -- a stale read
        # (the file hasn't been touched by the current child yet, or
        # somehow still reflects an old one) must never count.
        if audio_state.get("generation") != current["generation"]:
            return
        if audio_state.get("is_blank") is not False:
            return  # still "starting" (null) or currently silent (true)
        since = audio_state.get("since")
        if since is None or (time.time() - since) < STABILIZATION_SECONDS:
            return
        self._stabilized[input_device] = True
        self._retry_index[input_device] = 0
        meta = self._group_meta(input_device)
        meta["last_successful_start"] = time.time()
        meta["consecutive_failures"] = 0
        self._log(input_device, f"Stable for {STABILIZATION_SECONDS}s+ (generation={current['generation']}) -- backoff reset.", force=True)
        self._write_group_state(input_device)

    def _check_health(self):
        now_mono = time.monotonic()

        # Dead processes -- handle each independently; with multiple
        # device groups, blocking here on one would delay noticing the
        # others (Phase 7 req #4/#12).
        for input_device, proc in list(self._procs.items()):
            if proc.poll() is not None:
                self._handle_exit(input_device, proc.returncode)

        # Retries whose backoff has elapsed -- re-read the current DB
        # state rather than reusing the encoder list from whenever this
        # group last started, in case rows changed since.
        for input_device, at in list(self._retry_at.items()):
            if now_mono < at:
                continue
            del self._retry_at[input_device]
            close_old_connections()
            groups = _group_by_input_device(Encoder.objects.filter(enabled=True))
            encoders = groups.get(input_device)
            if not encoders:
                # Phase 7 req #10/#11: a group with no enabled encoders
                # left must not be resurrected -- and its retry/backoff
                # bookkeeping is dropped entirely, not just skipped this
                # once, so a LATER re-enable starts the backoff schedule
                # fresh rather than resuming mid-escalation from a retry
                # sequence that's since become meaningless.
                self._log(input_device, "No enabled encoders left for this device, not restarting.", force=True)
                self._retry_index.pop(input_device, None)
                self._meta.pop(input_device, None)
                try:
                    _group_state_path(input_device).unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            self._log(input_device, "Retrying...", force=True)
            if not self._start_group(input_device, encoders):
                self._schedule_retry(input_device)

        # Stabilization check for every currently-running child.
        for input_device in list(self._procs.keys()):
            self._check_stabilization(input_device)

        # Heartbeat: refresh every currently-tracked group's state file
        # on every tick, not just when something changed -- this file's
        # own `timestamp` is the tight-staleness "is the manager loop
        # itself alive and still supervising this group" signal
        # monitoring relies on (Phase 8 req #4). A long, uneventful
        # healthy run must not let it go stale just because nothing
        # happened.
        for input_device in list(self._procs.keys()):
            self._write_group_state(input_device)
