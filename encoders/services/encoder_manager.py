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
import os
import re
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
from isadoraair.version_info import capture_runtime_commit  # noqa: E402

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

# Absolute path, not looked up via PATH -- upstream nu774/fdkaac built
# from source under /usr/local (see _format_block's own comment for
# why Ubuntu's packaged fdkaac/liquidsoap's %fdkaac binding are both
# unsuitable). Named here (Phase 2 hardening) rather than left as two
# separately hardcoded literals in _format_block and
# _aircheck_format_block, so encoders/services/preflight.py's
# dependency check can verify the EXACT path the generated script will
# actually invoke, not a PATH-based guess that could disagree with it.
FDKAAC_PATH = "/usr/local/bin/fdkaac"

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

# --- Phase 2 hardening: candidate configuration qualification ---------
# How long evaluate_encoder_group_health's aggregate must continuously
# report "ok" (which already itself requires STABILIZATION_SECONDS of
# stable audio, all configured Shoutcast SIDs up, correct generation,
# fresh manager/audio state) before a CANDIDATE configuration is
# trusted enough to become the new last-known-good. Deliberately ON
# TOP OF, not instead of, the stabilization already baked into "ok" --
# defense against a borderline-flapping condition at the boundary
# (e.g. a SID that flickers up/down right at the edge of "ok"),
# without re-measuring the same 25s twice.
CANDIDATE_QUALIFICATION_SECONDS = 30

# Overall wall-clock budget from candidate launch to required
# qualification. Generous margin over the sum of the startup
# classifier's own resolution window (up to ~10s), STABILIZATION_
# SECONDS, and CANDIDATE_QUALIFICATION_SECONDS -- ordinary ALSA/
# Shoutcast connection jitter must not trip a false qualification
# failure, but a candidate that genuinely can't prove itself must not
# be allowed to sit in probation forever either.
CANDIDATE_QUALIFICATION_DEADLINE_SECONDS = 150

# The encoder manager checking its OWN systemd unit's ActiveState is
# not circular -- systemctl queries system-wide unit state independent
# of which process asks, and it's a real, additional signal (systemd
# considers this process alive/active), the same one probe_systemd
# already gives the dashboard. Matches deploy/isadoraair-encoders.service.
ENCODER_MANAGER_SYSTEMD_UNIT = "isadoraair-encoders.service"

# Shared with the startup classifier's own dB_levels() comparison in
# build_liquidsoap_script (2026-08-06) -- ONE threshold literal, not
# two independently-maintained copies that could silently drift apart.
BLANK_DETECT_THRESHOLD_DB = -40.0

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
    r"""Escape a value for a Liquidsoap string literal.

    Phase 2 hardening finding (confirmed live, not assumed): Liquidsoap
    double-quoted string literals support `#{expr}` interpolation --
    `y = "hello #{x} world"` genuinely evaluates `x` as a real
    expression when the script runs (verified via a standalone
    `liquidsoap` run). There is no backslash escape for it -- `\#` is a
    parse error, not an escape (also verified live) -- so unlike `\`
    and `"`, this can't be neutralized by adding another .replace()
    call; the only correct response is refusing to embed it at all.
    encoders/services/validation.py already rejects any Encoder field
    containing "#{" before a row can reach candidate rendering; this is
    the second, independent gate -- a hard failure here, not a silent
    strip/replace, for anything that reaches this function without
    having gone through that validation."""
    value = value or ""
    if "#{" in value:
        raise ValueError('Refusing to embed "#{" in a Liquidsoap string literal (interpolation injection risk).')
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_GENERATION_LINE_PATTERN = re.compile(r'^generation = "[^"]*"$', re.MULTILINE)


def _substitute_generation(script_text, new_generation):
    """Replace a rendered script's baked-in `generation = "..."` line
    (see build_liquidsoap_script) with a fresh one. Used exclusively
    for rollback (Phase 2J): the persisted LKG script text was
    rendered at PROMOTION time with whatever generation was live then
    -- relaunching it verbatim would reuse a stale generation the
    write_state() CAS guard (see build_liquidsoap_script's own
    comments) would then reject every write against, since a NEWER
    entry could already exist in the audio-state file from the
    candidate that just failed. Every other property of the script
    (encoders, aircheck/telnet, credentials) is exactly what was
    proven to work and must NOT be touched -- only this one line.

    Raises ValueError if the expected line isn't found exactly once --
    a script that doesn't match this shape is not safe to relaunch
    blindly; better to fail loudly here than silently launch something
    that was never actually rendered by build_liquidsoap_script."""
    matches = _GENERATION_LINE_PATTERN.findall(script_text)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one 'generation = \"...\"' line in the script, found {len(matches)} -- "
            "refusing to relaunch a script that doesn't match the expected shape."
        )
    return _GENERATION_LINE_PATTERN.sub(f'generation = {_liq_string(new_generation)}', script_text, count=1)



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
    """Write-tmp-then-atomic-replace -- same idiom already used by
    monitoring/services/monitor.py's _write_state/_write_listener_state,
    strengthened here (2026-08-05 pre-deploy review) with an explicit
    flush+fsync on the temp file before the rename, os.replace() rather
    than Path.rename(), and cleanup of the temp file on ANY failure.

    No reader (a monitoring poll landing mid-write) can ever observe a
    partially-written or truncated state file -- the destination path
    is never touched until the single atomic replace call, and that
    call either fully succeeds (new complete document visible) or
    fully fails (old complete document, or nothing, still there).

    os.replace() vs. the old Path.rename(): equivalent in practice on
    this project's only real deployment target (Linux -- POSIX
    rename(2) already atomically replaces an existing destination) --
    switched to os.replace() anyway since it's the API that's
    *guaranteed* to have that behavior on every platform, not just
    this one, making the intent unambiguous at the call site rather
    than relying on a platform-specific guarantee.

    fsync is on the TEMP FILE's own descriptor, before the rename --
    these files live on tmpfs (/run/isadoraair has no on-disk
    durability to guarantee regardless of fsync), so this isn't a
    crash-durability claim; it just guarantees the write is fully out
    of any buffering layer before the name swap that makes it visible,
    same spirit as the flush() this already implied.

    On any failure (a full tmpfs, a permissions problem, a caller
    passing non-JSON-serializable data) the half-written .tmp file is
    removed before re-raising -- repeated failures (e.g. every 5s from
    the manager's own heartbeat) must never accumulate stray fragments
    on tmpfs, and the caller still learns the write failed rather than
    this function silently swallowing it."""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
        #
        # Roadmap 3.10: mp3_rate_mode ("auto"/"cbr"/"abr") lets an
        # operator (or a provider preset's validation, e.g. Live365/
        # Radio.co requiring effective CBR) override the bitrate-based
        # default below. effective_mp3_rate_mode (encoders/services/
        # validation.py) is the ONE place that resolves "auto" -- reused
        # here rather than re-implementing the >=192 threshold a second
        # time, so the renderer and validate_provider_policy's CBR check
        # can never silently disagree about what "auto" actually renders
        # for a given bitrate. "cbr"/"abr" bypass the threshold entirely,
        # exactly per the roadmap's stated semantics.
        from .validation import effective_mp3_rate_mode
        br = encoder.bitrate_kbps
        if effective_mp3_rate_mode(encoder) == "cbr":
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
            f"{FDKAAC_PATH} -R --raw-channels 2 --raw-rate 44100 "
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


def _dest_key(encoder, index):
    """Stable-enough per-destination identifier for the connection-state
    tracking wired up below -- used both as a Liquidsoap identifier
    fragment (dest_connected_<key>, output_<key>, ...) and, stringified,
    as the "id" written into the audio-state JSON's "destinations" list
    (matched back by monitoring.services.probes.evaluate_encoder_group_
    health against str(Encoder.id)).

    Falls back to a positional index only when encoder.id is None (an
    in-memory Encoder(...) never saved to the DB) -- exercised by this
    project's own test suite, which builds unsaved instances directly to
    check the renderer's output shape. Every REAL launch (_start_group,
    always fed Encoder.objects.filter(...) rows -- script_override/
    rollback bypasses build_liquidsoap_script entirely and never calls
    this) has a real pk by construction, so this fallback never actually
    matters for a running destination-health decision."""
    return encoder.id if encoder.id is not None else f"idx{index}"


def _destinations_snapshot_expr(dest_keys):
    """A Liquidsoap list-of-records EXPRESSION (a string of Liquidsoap
    source code, not a Python value) snapshotting every destination's
    CURRENT dest_connected_<key>/dest_since_<key> ref pair -- embedded
    into write_state's own body (see build_liquidsoap_script) so every
    write (a real on_connect/on_disconnect transition, the periodic
    heartbeat, or the startup classifier) carries every destination's
    latest known state, not just whichever one just changed.

    `id` is ALWAYS rendered as a Liquidsoap STRING (via _liq_string),
    even for the common case where dest_key is a real integer Encoder
    id -- deliberately, so a single script mixing a real-pk encoder
    with a fallback "idxN" key (only possible via this project's own
    test suite building unsaved Encoder(...) instances, see _dest_key)
    never produces a Liquidsoap list-literal TYPE ERROR from mixing int
    and string entries for the same record field; every real production
    launch only ever has real integer ids anyway, so this costs nothing
    there. monitoring.services.probes.evaluate_encoder_group_health
    matches back via str(Encoder.id), the same convention Encoder.
    shoutcast_sid already established for exactly this kind of
    cross-language (Python int <-> Liquidsoap/JSON) matching.

    Falls back to an explicitly-typed empty list (confirmed live via a
    standalone json.stringify probe during this feature's development
    -- an untyped `[]` alone risks Liquidsoap being unable to resolve
    its element type) for the (production-impossible, test-only) case
    of a zero-encoder group."""
    if not dest_keys:
        return "([] : [{id: string, connected: bool?, since: float?}])"
    entries = ", ".join(
        f'{{id = {_liq_string(str(key))}, connected = dest_connected_{key}(), since = dest_since_{key}()}}'
        for key in dest_keys
    )
    return f"[{entries}]"


def _output_block(encoder, source_var, dest_key):
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
        output_expr = (
            f"output.icecast({fmt}, {common}, "
            f"mount={_liq_string(encoder.mount)}, "
            f"user={_liq_string(encoder.username)}, {source_var})"
        )
    elif encoder.protocol == "shoutcast2":
        # Phase 2 hardening: icy_id must come from a PARSED integer, never
        # raw DB text substituted directly into the script -- normalize_
        # shoutcast_sid() raises ConfigValidationError for anything that
        # isn't a clean positive integer (blank, 0, negative, non-numeric,
        # multiple slashes, embedded script fragments). This is a second,
        # independent gate: the candidate-validation pipeline (encoders/
        # services/validation.py) already rejects an invalid SID before a
        # row ever reaches candidate rendering, but script generation
        # itself must not trust that gate was actually applied upstream --
        # a hard failure here is the correct, safe behavior for a row that
        # somehow arrived without having been validated, not a silent
        # fallback.
        from .validation import normalize_shoutcast_sid
        sid = normalize_shoutcast_sid(encoder.mount)
        output_expr = f"output.shoutcast({fmt}, {common}, icy_id={sid}, {source_var})"
    else:
        output_expr = f"output.shoutcast({fmt}, {common}, {source_var})"  # shoutcast1

    # Roadmap 3.10: generic Liquidsoap destination connection-state
    # signal -- one per output, regardless of protocol/provider (Icecast,
    # Shoutcast 1/2, any provider preset on top). This is what lets
    # monitoring/services/probes.py's evaluate_encoder_group_health tell
    # "process alive + audio present" apart from "...and this specific
    # destination has actually established its connection" for Icecast/
    # Live365/Radio.co destinations, which have no external DNAS
    # /statistics interface to independently verify against (see that
    # function's own docstring). `.on_connect`/`.on_disconnect` are
    # output.icecast/output.shoutcast SOURCE METHODS (confirmed live via
    # `liquidsoap --list-functions-md` against this box's installed
    # 2.4.0+dev, and against a synthetic full script via
    # `liquidsoap --check` -- NOT the deprecated same-named constructor
    # arguments) -- same synchronous=false + named-handler-function shape
    # already established by source.on_blank/source.on_noise above, and
    # each handler re-invokes write_state() so a connect/disconnect
    # transition is reflected in the SAME generation-CAS-guarded state
    # file immediately, not just at the next 60s heartbeat. Initialized
    # to ref(null) -- "not yet observed," matching this file's existing
    # is_blank three-state philosophy -- never an optimistic default;
    # never connected at all (a wrong password, say) correctly stays
    # null/false forever, never silently reads as healthy.
    # No initial-connect race despite output.icecast/output.shoutcast's
    # default start=true and the callbacks being registered on the NEXT
    # lines, after the output value already exists (2026-08-11 pre-
    # commit review): confirmed EMPIRICALLY, not assumed, with two
    # isolated smoke tests against this box's real installed 2.4.0+dev
    # (synthetic noise() source, no ALSA device or production process
    # touched) -- neither is committed, both are reproducible from this
    # comment's description. Test 1: an output.icecast targeting a
    # guaranteed-closed local port, with a `setup_done` ref set as the
    # LAST line after this SAME output's on_connect/on_disconnect/
    # on_error registration. Liquidsoap's own log confirms the ordering
    # directly: "Evaluating main script" completes and "Starting
    # top-level clock" only happens AFTER that -- i.e. no output's
    # clock ticks (and nothing can attempt to connect) until the ENTIRE
    # script has finished evaluating, which is strictly after every
    # output's own callback registration (ordinary sequential
    # statements in the same synchronous evaluation pass). The
    # subsequent connection attempt's on_error fired with setup_done
    # already true. Test 2: a full connect -> disconnect -> reconnect
    # lifecycle against a real local TCP listener completed exactly as
    # expected (initial connected=false -> on_connect fires,
    # connected=true -> listener killed -> on_disconnect fires,
    # connected=false -> listener restarted -> on_connect fires again,
    # connected=true), with zero missed transitions. This generalizes
    # to N outputs in one script (this function's own multi-encoder
    # case): since evaluation is single-threaded and sequential, and
    # ALL outputs share the one global "clock start" event that only
    # fires after the complete script has evaluated, every output's
    # on_connect/on_disconnect is registered before ANY output's clock
    # can tick, regardless of how many destinations are in this group.
    # See test_output_block_registers_callbacks_immediately_after_
    # output_construction in encoders/tests/test_encoder_manager.py,
    # which locks in the ORDERING this proof depends on (output
    # construction immediately followed by ITS OWN callback wiring,
    # never deferred past a later destination's own construction).
    out_var = f"output_{dest_key}"
    return [
        f'{out_var} = {output_expr}',
        f'def dest_connect_{dest_key}() =',
        f'  dest_connected_{dest_key}.set(true)',
        f'  dest_since_{dest_key}.set(time())',
        f'  write_state(last_status(), last_is_blank(), since_ts())',
        'end',
        f'def dest_disconnect_{dest_key}() =',
        f'  dest_connected_{dest_key}.set(false)',
        f'  dest_since_{dest_key}.set(time())',
        f'  write_state(last_status(), last_is_blank(), since_ts())',
        'end',
        f'{out_var}.on_connect(synchronous=false, dest_connect_{dest_key})',
        f'{out_var}.on_disconnect(synchronous=false, dest_disconnect_{dest_key})',
        '',
    ]


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
            f"{FDKAAC_PATH} -R --raw-channels 2 --raw-rate 44100 "
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
    # Materialized once, up front -- dest_keys (below) and the final
    # output-block loop both iterate `encoders` independently, and a
    # QuerySet re-iterating is fine but a one-shot generator would not
    # be; every real caller already passes a list (or a QuerySet, which
    # list() below simply snapshots), so this is a defensive no-op in
    # practice, not a behavior change.
    encoders = list(encoders)
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
        # start_blank left at its documented default (false = assume
        # noise, not silence, from the first frame) -- DELIBERATELY not
        # overridden. 2026-08-06 post-deployment investigation, round 2:
        # start_blank=true (this file's own prior fix) turned out to
        # have a worse, opposite bug -- confirmed empirically with an
        # isolated synthetic script, a genuinely continuously-SILENT
        # source spuriously fired on_noise (a FALSE "audio_ok") within
        # ~0.05s, every time, reproducibly. That's a false-positive
        # "healthy" report for real silence -- exactly the failure mode
        # this whole project exists to eliminate, and worse than the
        # original bug (which only ever left state stuck "unknown",
        # never falsely "healthy"). Neither start_blank value alone
        # correctly resolves BOTH cold-start shapes (continuous audio,
        # continuous silence) -- see the one-shot startup classifier
        # below, which does, without depending on either default.
        f'source = blank.detect(threshold={BLANK_DETECT_THRESHOLD_DB}, max_blank=20.0, min_noise=0.5, source)',
        '',
        f'generation = {_liq_string(generation)}',
        f'input_device_str = {_liq_string(input_device)}',
        # Liquidsoap's own wall-clock time as of the first line actually
        # executing -- more accurate than a Python-side pre-Popen()
        # timestamp, which would be measured slightly before this
        # process even exec'd.
        'started_at = time()',
        '',
    ]
    # Roadmap 3.10: one pair of per-destination connection-state refs per
    # encoder in this group, declared here -- BEFORE write_state's own
    # definition below, since its body reads them to build the
    # "destinations" snapshot on every write. Liquidsoap resolves names
    # lexically at the point a `def`/`let` appears, same as every other
    # forward-reference constraint already respected elsewhere in this
    # function (e.g. write_state itself must be defined before on_blank/
    # on_noise/the classifier/the heartbeat, all of which call it) --
    # the ACTUAL output.icecast()/output.shoutcast() objects and their
    # .on_connect/.on_disconnect wiring (_output_block) can and do stay
    # in their existing position at the very end of the script; they only
    # need write_state and these refs to already be in scope, not the
    # other way around.
    #
    # ref(null) (untyped-until-first-.set, same idiom as last_is_blank
    # above and confirmed live via a standalone json.stringify probe
    # during this feature's own development) -- "not yet observed,"
    # never an optimistic default; a destination that never connects
    # correctly stays null/false in the state file forever, never reads
    # as healthy just because nothing failed loudly.
    dest_keys = [_dest_key(enc, i) for i, enc in enumerate(encoders)]
    for key in dest_keys:
        lines += [
            f'dest_connected_{key} = ref(null)',
            f'dest_since_{key} = ref(null)',
        ]
    lines += [
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
        # Generation CAS guard lives HERE, centrally, so EVERY write
        # site (this script's own initial write, on_blank, on_noise,
        # the 60s heartbeat, and the startup classifier below) is
        # uniformly protected against a lingering, already-superseded
        # process still writing into a file a NEWER generation now
        # owns -- 2026-08-06 post-deployment investigation, round 2.
        # Confirmed empirically this matters beyond just the
        # classifier: thread.run(every=X) with no explicit delay=
        # defaults delay=0.0, meaning the 60s heartbeat's FIRST tick
        # fires almost immediately at script start, not after the
        # first 60s interval -- so an old, still-alive process's own
        # heartbeat (not just its classifier) can race a newer
        # generation's takeover of the same file within roughly the
        # first second of the old process still being alive. A single
        # guard inside write_state() closes that for every caller at
        # once, rather than requiring each call site to reimplement
        # its own check (the classifier's first draft did exactly
        # that, redundantly, before this consolidation).',
        'def write_state(status, is_blank, since) =',
        f'  file_ok = if file.exists({_liq_string(audio_state_path)}) then',
        '    (try',
        f'      current_text = file.contents({_liq_string(audio_state_path)})',
        # Checked separately, deliberately -- NOT one combined
        # substring, because this file has TWO different writers with
        # two different JSON formatters: this script's own
        # json.stringify(compact=true, ...) (no space after ":") for
        # every write AFTER the first, but the Python-side
        # _atomic_write_json (encoder_manager.py, plain json.dumps(),
        # default separators) writes the PRE-SPAWN placeholder every
        # generation starts from -- WITH a space after ":". A single
        # exact-substring check tuned to only one of those two formats
        # would silently fail open against whichever format it wasn't
        # tuned for (caught empirically: an earlier version of this
        # guard, checked only against the no-space format, always
        # treated a real Python-written value as "no generation key
        # present at all" and let every write through regardless of
        # whether the generation actually matched -- exactly the
        # scenario this guard exists to catch). "Does the key exist"
        # doesn't need this distinction (":" itself is never preceded
        # by a space in valid JSON from either writer); only the
        # value-match check does.
        '      has_generation_key = string.contains(substring="\\"generation\\":", current_text)',
        '      matches_ours = string.contains(substring="\\"generation\\":\\"#{generation}\\"", current_text)',
        '        or string.contains(substring="\\"generation\\": \\"#{generation}\\"", current_text)',
        '      matches_ours or (not has_generation_key)',
        '    catch _ do',
        '      true',  # unreadable, not a generation mismatch -- fail open, matching this file's existing "never lose track of state over a read hiccup" precedent elsewhere
        '    end : bool)',
        '  else',
        '    true',
        '  end',
        '  if file_ok then',
        '    last_status.set(status)',
        '    last_is_blank.set(is_blank)',
        '    since_ts.set(since)',
        f'    destinations = {_destinations_snapshot_expr(dest_keys)}',
        '    state = json.stringify(compact=true, {',
        '      status = status,',
        '      is_blank = is_blank,',
        '      audio_observed = audio_observed(),',
        '      input_device = input_device_str,',
        '      pid = process.pid(),',
        '      generation = generation,',
        '      started_at = started_at,',
        '      since = since,',
        '      timestamp = time(),',
        '      destinations = destinations,',
        '    })',
        # temp_dir must live on the same filesystem as the target for the
        # atomic rename to succeed. Without this, liquidsoap defaults temp
        # to /tmp and logs "Atomic rename failed!" on every write (harmless
        # -- the write still happens non-atomically -- but with the 60s
        # heartbeat it turns into once-a-minute log spam).
        f'    file.write(data=state, atomic=true, temp_dir="/run/isadoraair", {_liq_string(audio_state_path)})',
        '  end',
        'end',
        '',
        # is_blank=null on the transition writes below would be WRONG --
        # on_blank/on_noise firing means blank.detect has definitively
        # observed something real, so null (meaning "not yet verified")
        # never belongs on either of these two lines, only on the
        # startup line further down.
        # real_transition_seen is set ONLY by a genuine blank.detect
        # transition callback firing -- deliberately a separate flag
        # from last_status, which the startup classifier below ALSO
        # writes through (see that block's own comment for why
        # conflating the two would break the classifier's ability to
        # keep re-measuring after its own first write).
        'real_transition_seen = ref(false)',
        'source.on_blank(synchronous=false, {',
        '  real_transition_seen.set(true)',
        '  write_state("silent", true, time())',
        '})',
        'source.on_noise(synchronous=false, {',
        '  real_transition_seen.set(true)',
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
        '',
        # Startup classifier (2026-08-06 post-deployment investigation,
        # round 2). Neither blank.detect start_blank value alone
        # resolves BOTH cold-start shapes correctly: start_blank=false
        # (the default, used above) never fires on_noise for audio
        # that's already continuous from frame one (no blank->noise
        # transition to observe -- the ORIGINAL bug); start_blank=true
        # was tried and reverted -- confirmed empirically to spuriously
        # fire on_noise within ~0.05s for genuinely continuous SILENCE
        # too (a false "audio_ok", worse than the original bug).
        # Rather than depend on either, this independently measures the
        # real, current dB level via blank.detect's own `dB_levels()`
        # method (confirmed empirically to be an accurate, real-time
        # measurement, unlike `is_blank()`, which just mirrors the same
        # transition-state-machine the callbacks use and is exactly as
        # stale).
        #
        # Deliberately keeps RE-measuring (not a single fire-once
        # classification) for its whole retry window: blank.detect's
        # own transition machinery needs a FULL max_blank (20s) of
        # continuous silence before it can register a blank->noise
        # transition at all, so a brief, transient silence at startup
        # shorter than that -- immediately followed by real audio --
        # would otherwise leave the classifier's first (correct, at the
        # time) "silent" snapshot stuck with nothing left to ever
        # correct it (confirmed empirically: this exact shape was caught
        # by this file's own test suite before landing). Re-measuring
        # only WRITES when the classification actually changes (never
        # on every tick), so it doesn't spuriously reset the dashboard's
        # "stable for" clock the way an unconditional re-write would.
        #
        # Safety properties (see encoders/tests/test_encoder_manager.py
        # StartupClassifierScriptTests / the synthetic investigation
        # for the empirical proof of each):
        #   - A real on_blank/on_noise transition always supersedes:
        #     gated on real_transition_seen(), a flag ONLY the genuine
        #     callbacks above ever set -- never the classifier itself,
        #     so the classifier keeps working (and immediately stops
        #     the moment a real transition fires) without being
        #     confused by its own prior writes into last_status.
        #   - Never writes an optimistic default -- only ever writes
        #     once it has an ACTUAL non-null level measurement, and
        #     only when that measurement's classification differs from
        #     what it last wrote.
        #   - Retries every 0.5s (bounded to 20 attempts, 10s total)
        #     rather than a single fixed delay, so it isn't sensitive
        #     to real-world ALSA/dsnoop open latency (input.alsa can
        #     take over a second to start delivering frames) the way a
        #     single guess at a delay would be.
        #   - Generation-guarded against a lingering old-generation
        #     process's classifier firing late and clobbering a NEWER
        #     generation's already-current state: write_state() itself
        #     now carries this guard (see its own comment above) --
        #     the classifier doesn't need its own copy. A stale write
        #     from a superseded generation is silently dropped there,
        #     never applied, and classify_last_status still advances
        #     regardless (a superseded generation's file can never
        #     become "ours" again, so there's nothing useful a retry
        #     would accomplish).
        'classify_last_status = ref("")',
        'classify_attempts = ref(0)',
        'def classify_startup() =',
        '  if (not real_transition_seen()) and (classify_attempts() < 20) then',
        '    classify_attempts.set(classify_attempts() + 1)',
        '    levels = source.dB_levels()',
        '    if levels != null then',
        '      lv = null.get(levels)',
        '      max_level = list.fold(fun (acc, x) -> if x > acc then x else acc end, -1000.0, lv)',
        f'      new_status = if max_level > {BLANK_DETECT_THRESHOLD_DB} then "audio_ok" else "silent" end',
        '      if new_status != classify_last_status() then',
        '        classify_last_status.set(new_status)',
        '        if new_status == "audio_ok" then',
        '          audio_observed.set(true)',
        '          write_state("audio_ok", false, time())',
        '        else',
        '          write_state("silent", true, time())',
        '        end',
        '      end',
        '    end',
        '  end',
        'end',
        'thread.run(delay=1.0, every=0.5, classify_startup)',
        '',
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
    for encoder, key in zip(encoders, dest_keys):
        lines += _output_block(encoder, "source", key)
    if host_aircheck:
        lines += _aircheck_block()
    return "\n".join(lines) + "\n"


class EncoderManager:
    def __init__(self):
        # Release/version-skew visibility (1.7 roadmap item): captured
        # exactly ONCE, here, at process construction -- see
        # isadoraair/version_info.py's own docstring for why this must
        # never be recomputed later. There is no manager-level/process-
        # wide heartbeat file for this service (only the per-group files
        # written by _write_group_state below), so this same value is
        # stamped redundantly into every group's state file -- they all
        # describe this one process, so any one of them is sufficient
        # for the monitoring side to read.
        self._runtime_commit = capture_runtime_commit()
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

        # --- Phase 2 hardening: candidate/LKG configuration state ---
        # input_device -> "accepted" | "candidate" | "rollback". Set the
        # moment a launch succeeds (_launch_group), read by _handle_exit
        # (a crash during "candidate"/"rollback" means qualification
        # failed -- route to rollback/critical, NOT the ordinary retry a
        # crash of an already-"accepted" child gets) and by
        # _check_candidate_qualification (only tracks generations
        # currently "candidate" or "rollback").
        self._launch_kind = {}
        # input_device -> monotonic time.time() when the CURRENT
        # generation's evaluate_encoder_group_health first read "ok",
        # or None if not currently (or not yet) continuously healthy.
        # Reset to None the moment health stops being "ok" for this
        # generation -- CANDIDATE_QUALIFICATION_SECONDS must be
        # CONTINUOUS, not cumulative.
        self._qualify_started_at = {}
        # input_device -> the generation string _qualify_started_at is
        # tracking -- lets _check_candidate_qualification tell "still
        # the same candidate I was timing" apart from "a new generation
        # appeared (crash+relaunch) since my last tick" without relying
        # on wall-clock alone.
        self._qualify_generation = {}
        # input_device -> deadline (time.monotonic()) by which
        # CANDIDATE_QUALIFICATION_SECONDS of continuous health must be
        # reached, else qualification is deemed failed.
        self._qualify_deadline = {}
        # input_device -> set of fingerprints (encoders.services.lkg.
        # compute_fingerprint) that have already failed candidate
        # qualification (validation, preflight, OR live) and must not
        # be silently auto-retried -- Phase 2K's "keep the rejected
        # configuration from being automatically retried forever."
        # Cleared for a slug only when a DIFFERENT fingerprint is next
        # attempted (a distinct admin edit is allowed a fresh try).
        self._rejected_fingerprints = {}
        # input_device -> True once BOTH a candidate AND its rollback
        # have failed -- Phase 2K's "do not endlessly bounce
        # candidate -> LKG -> candidate -> LKG." While True, _launch_
        # group falls back to plain LKG relaunches via the ordinary
        # retry/backoff loop only (infrastructure recovery), with no
        # further automatic configuration switching, until either the
        # DB configuration changes to a fingerprint not already in
        # _rejected_fingerprints, or the process restarts.
        self._critical_stopped = {}
        # input_device -> fingerprint of the candidate/rollback
        # currently on probation (None once accepted or cleared).
        self._candidate_fingerprint = {}
        # input_device -> the CANDIDATE's own encoders list (real
        # Encoder rows) -- ONLY used for candidate-specific bookkeeping:
        # the qualification-failed event's encoder_names, and
        # _promote_candidate's encoder_ids/encoder_names/destinations
        # metadata. Deliberately NEVER read to decide what a health
        # check should verify -- see _qualify_expected below for that
        # (post-review fix: an earlier draft read THIS dict inside
        # _check_candidate_qualification even during a rollback, which
        # meant rollback health was evaluated against the REJECTED
        # candidate's SID/host/port -- e.g. "is SID 5 up?" -- instead of
        # what was actually relaunched, the LKG's own "is SID 1 up?".
        # Confirmed as a real, live bug during review, not theoretical
        # -- see test_candidate_qualification.py's
        # RollbackQualificationExpectedEncodersTests).
        self._candidate_encoders = {}
        # input_device -> the encoder-like objects (real Encoder rows
        # for "candidate"; lightweight, LKG-metadata-derived stand-ins
        # for "rollback" -- see _lkg_destinations_to_expected) that
        # _check_candidate_qualification should ask evaluate_encoder_
        # group_health about. This is deliberately NEVER re-derived
        # from the live DB for the rollback case: the DB may hold
        # exactly the rejected desired configuration that's being
        # rolled back FROM (a re-fetch by ID would silently reintroduce
        # the same bug if the operator edited the SAME row in place
        # rather than creating a new one) -- only the LKG's own frozen,
        # already-proven snapshot is trustworthy here.
        self._qualify_expected = {}

        # --- Phase 3 hardening: per-input-device reconciliation ---------
        # input_device -> fingerprint (encoders.services.lkg.
        # compute_fingerprint) of whatever configuration this group's
        # CURRENT live child actually represents -- set explicitly at
        # every successful launch call site (never re-derived by
        # guessing from launch_kind, and never computed by hashing
        # _running_encoders directly: an "accepted"-via-LKG-fallback or
        # "rollback" launch's encoders are lightweight LKG-metadata
        # stand-ins that don't carry every RUNTIME_AFFECTING_FIELDS
        # attribute compute_fingerprint needs -- the LKG's OWN recorded
        # fingerprint is what's actually running in that case, and each
        # call site already knows it). Cleared to None (via .pop)
        # everywhere self._current is also cleared -- "no live child"
        # must mean "no running fingerprint" too, never a stale value.
        # This is THE reconciliation signal: _reconcile() compares this
        # against a freshly-computed desired fingerprint every tick, so
        # "has this group's desired configuration actually changed
        # relative to what's REALLY running right now" never has to
        # guess from launch_kind alone (see this module's Phase 3
        # docstring addendum below for why launch_kind alone isn't
        # enough once rapid DB edits during a candidate's probation are
        # a normal, expected occurrence rather than a rare manual-shell
        # edge case).
        self._running_fingerprint = {}
        # input_device -> the exact encoder-like objects (real Encoder
        # rows, or destinations_from_lkg_meta stand-ins) that were
        # passed to the launch that produced the CURRENT live child --
        # set generically inside _start_group itself (the one place
        # that already receives exactly this list for every launch
        # kind), cleared alongside _running_fingerprint. Used ONLY for
        # Phase 3M cross-group destination-collision checks (via
        # encoders.services.validation.normalized_destination_key) --
        # never for qualification (see _qualify_expected above, a
        # separate, narrower-scoped dict for that).
        self._running_encoders = {}
        # input_device -> the persisted LKG's own fingerprint, cached
        # in-memory to avoid a disk read on every 5s heartbeat tick.
        # Refreshed at every _launch_group call (which already reads
        # the LKG fresh for its own fast-path comparison, at zero extra
        # I/O cost) and at every successful _promote_candidate -- this
        # process is the ONLY writer of the LKG, so the cache can never
        # actually go stale relative to what's on disk.
        self._accepted_fingerprint = {}
        # input_device -> the fingerprint of the current enabled DB
        # configuration, refreshed every reconciliation tick (see
        # _reconcile) for every desired group, including unchanged
        # ones -- purely a read-through cache so _write_group_state's
        # heartbeat always has a fresh value without recomputing it
        # itself.
        self._desired_fingerprint = {}
        # input_device -> True while the CURRENT candidate/rollback in
        # flight for this group was launched by Phase 3 reconciliation
        # (_reconcile_changed_group), never for an ordinary Phase 2
        # cold-boot/retry candidate (_launch_group, called from
        # start()/_retry_group as well as reconciliation's own "new"-
        # group dispatch). Set ONLY at the one _reconcile_changed_group
        # launch site; read (and cleared) at the promotion/rollback
        # completion points below to decide whether THIS lifecycle's
        # eventual outcome belongs in the group-state JSON's
        # last_reconcile_result -- without this, an ordinary Phase 2
        # bootstrap promotion would silently inherit whatever stale
        # "candidate_launched"/etc. a PRIOR, unrelated reconciliation
        # attempt last wrote, misrepresenting reconciliation history
        # that never actually happened. Explicitly cleared (never left
        # to go stale) anywhere a candidate/rollback is torn down
        # outside its own normal promote/rollback-succeeded/rollback-
        # failed conclusion -- see _stop_group_intentionally and
        # _remove_group_intentionally.
        self._reconcile_origin = {}

    def start(self):
        self.running = True
        close_old_connections()
        SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        groups = _group_by_input_device(Encoder.objects.filter(enabled=True))
        for input_device, encoders in groups.items():
            if not self._launch_group(input_device, encoders):
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
            self._running_fingerprint.pop(input_device, None)
            self._running_encoders.pop(input_device, None)
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
            # Phase 3: eventual-apply-outcome fields for the group-state
            # JSON's own last_reconcile_* trio -- see
            # _record_reconcile_outcome.
            "last_reconcile_result": "", "last_reconcile_error": "", "last_reconcile_at": None,
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
        except Exception:
            pass

    def _write_group_state(self, input_device, next_retry_at=None):
        """Full snapshot write of the manager-owned per-group state
        file -- see _group_state_path. Always derives pid/generation/
        launched_at from self._current (empty dict if this group has no
        live child right now) rather than accepting them as parameters,
        so every call site can't accidentally write mismatched values.

        Phase 2M: launch_kind/critical_stopped are included so that a
        SEPARATE process -- the Django admin, running under gunicorn,
        never the encoder manager's own process -- can surface the
        desired (database) vs accepted (last-known-good) distinction
        without any direct in-memory link to this manager. This is the
        only channel available for that: admin.py cannot read
        self._launch_kind directly, it isn't in the same process."""
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
            "launch_kind": self._launch_kind.get(input_device, "accepted"),
            "critical_stopped": bool(self._critical_stopped.get(input_device)),
            # Phase 3B: desired/running/accepted, non-secret (fingerprints
            # are one-way hashes; see lkg.compute_fingerprint) -- the
            # channel a SEPARATE process (admin, monitoring) reads to
            # surface reconciliation status without guessing from
            # launch_kind alone.
            "desired_fingerprint": self._desired_fingerprint.get(input_device),
            "running_fingerprint": self._running_fingerprint.get(input_device),
            "accepted_fingerprint": self._accepted_fingerprint.get(input_device),
            "reconcile_status": self._reconcile_status(input_device),
            "last_reconcile_at": meta["last_reconcile_at"],
            "last_reconcile_result": meta["last_reconcile_result"],
            "last_reconcile_error": meta["last_reconcile_error"],
            "timestamp": time.time(),
            # 1.7 release/version-skew visibility -- fixed at process
            # start (see __init__), None if git was unavailable then.
            # Redundant across groups by design (see __init__ comment).
            "runtime_commit": self._runtime_commit,
        }
        try:
            _atomic_write_json(_group_state_path(input_device), state)
        except Exception as exc:
            print(f"  [{input_device}] Failed to write group state: {exc}")

    def _reconcile_status(self, input_device):
        """Phase 3O: a single, admin-readable summary of this group's
        current desired-vs-running-vs-accepted state, derived purely
        from already-tracked in-memory state (no extra I/O) -- one
        vocabulary shared by every reader instead of each one
        re-deriving its own interpretation of the raw fingerprint
        fields. See encoders/admin.py's _describe_group_status for how
        this is turned into an operator-facing message."""
        if self._critical_stopped.get(input_device):
            return "critical_stopped"
        kind = self._launch_kind.get(input_device, "accepted")
        if kind == "candidate":
            return "candidate_probation"
        if kind == "rollback":
            return "rollback_probation"
        desired = self._desired_fingerprint.get(input_device)
        running = self._running_fingerprint.get(input_device)
        if desired is None:
            # Not currently a desired group at all (mid-removal, or a
            # tick before this group's first-ever _reconcile() pass) --
            # not itself an error state, just nothing to compare yet.
            return "unknown"
        if desired == running:
            return "in_sync"
        if desired in self._rejected_fingerprints.get(input_device, set()):
            return "rejected"
        return "reconcile_pending"

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

    def _start_group(self, input_device, encoders, script_override=None):
        """Launch one Liquidsoap child for `input_device`. Returns True
        on a successful Popen(), False on failure -- EVERY call site is
        responsible for calling _schedule_retry(input_device) when this
        returns False (Phase 7 req #1: a failed initial launch must
        enter the same retry schedule a later child-exit would, not
        just print and silently give up on the group forever).

        script_override (Phase 2 hardening): when given, this EXACT
        text is launched (with a fresh generation substituted in via
        _substitute_generation) instead of rendering one from
        `encoders` via build_liquidsoap_script. This is how rollback
        (encoders/services/lkg.py's persisted LKG script) reaches the
        SAME launch/generation/state-tracking machinery every other
        launch already goes through, rather than a separate, parallel
        launch path that could drift from this one. `encoders` is
        still required even when overriding -- used for the log line
        and as the state-tracking record of what SHOULD be running,
        exactly as it always has been.

        Every filesystem write in this method (Phase 2 review-fix
        pass 2, Issue 3) is contained -- a disk/permission failure here
        must become an ordinary launch failure (False return, same
        retry/backoff/event machinery as a Popen() failure always
        used), never an exception escaping to the supervisor loop."""
        meta = self._group_meta(input_device)

        # file.watch() in the generated script throws an uncaught runtime
        # error if this file doesn't exist yet at the moment Liquidsoap
        # calls it -- guarantee it exists (placeholder is fine) before the
        # script is ever launched, not just on first-ever manager start.
        try:
            if not Path(NOW_PLAYING_PATH).is_file():
                # timestamp as a string, not a float -- see engine.py's
                # _write_now_playing() for why (metadata.json.parse() requires
                # a uniformly string-valued object).
                Path(NOW_PLAYING_PATH).write_text(
                    json.dumps({"title": "", "artist": "", "timestamp": str(time.time())}),
                    encoding="utf-8",
                )
        except OSError as exc:
            return self._record_launch_failure(input_device, meta, exc, "failed to initialize now-playing placeholder")

        generation = uuid.uuid4().hex[:12]
        if script_override is not None:
            try:
                script = _substitute_generation(script_override, generation)
            except ValueError as exc:
                return self._record_launch_failure(input_device, meta, exc, "failed to prepare rollback script")
        else:
            # Attach the aircheck output.file + telnet server only to the
            # main-air group (the one whose input_device matches
            # DEFAULT_INPUT_DEVICE). Rationale: two liquidsoap processes
            # can't bind the same telnet port, and aircheck records what
            # goes to air -- that's the DEFAULT_INPUT_DEVICE tap.
            host_aircheck = input_device == DEFAULT_INPUT_DEVICE
            script = build_liquidsoap_script(input_device, encoders, host_aircheck=host_aircheck, generation=generation)
        script_path = SCRIPT_DIR / f"encoders_{_slug(input_device)}.liq"
        try:
            script_path.write_text(script, encoding="utf-8")
        except OSError as exc:
            return self._record_launch_failure(input_device, meta, exc, "failed to write encoder script")

        launched_at = time.time()

        # Invalidate any stale audio-state file a PREVIOUS generation
        # left behind BEFORE spawning -- not after. (2026-08-06
        # post-deployment investigation: moved from after Popen() to
        # before it. The previous ordering left a real, if apparently
        # never-yet-triggered, window open: Popen() returning does not
        # mean the child has done anything yet, but this write used to
        # happen AFTER Popen() -- so if the child ever managed to open
        # its ALSA source and fire on_noise fast enough to publish its
        # OWN "audio_ok" for this exact generation before the line
        # below ran, that legitimate child-published state would have
        # been silently clobbered back to "starting"/null, and -- since
        # blank.detect only re-fires on a further TRANSITION, not on
        # continuously-unchanged noise -- would never self-correct
        # except via the child's own 60s heartbeat (which mirrors its
        # own in-memory belief, not this file, so it WOULD eventually
        # self-heal, just not for up to 60s). Writing this BEFORE
        # Popen() removes the window entirely rather than narrowing it:
        # the file already reflects the new generation before the
        # child exists to race against. pid is null here -- not yet
        # known -- and filled in below only if the child hasn't already
        # overtaken this placeholder.
        try:
            _atomic_write_json(_audio_state_path(input_device), {
                "status": "starting", "is_blank": None, "audio_observed": False,
                "input_device": input_device, "pid": None, "generation": generation,
                "started_at": launched_at, "since": launched_at, "timestamp": launched_at,
            })
        except Exception as exc:
            self._log(input_device, f"Failed to pre-invalidate previous audio-state file: {exc}", force=True)

        try:
            proc = subprocess.Popen(["liquidsoap", str(script_path)])
        except Exception as exc:
            return self._record_launch_failure(input_device, meta, exc, "Popen failed")

        # Popen() succeeded -- record the real pid now that we have it.
        # CONDITIONAL, not a blind overwrite: only fill in the pid if
        # the file still holds OUR OWN just-written "starting"
        # placeholder for this exact generation (pid still null). If
        # the child has already advanced past it for the SAME
        # generation (published its own audio_ok/silent, complete with
        # its own process.pid()), that state is authoritative and must
        # not be regressed back to "starting" -- the entire point of
        # this investigation.
        current = self._read_audio_state(input_device)
        if current.get("generation") == generation and current.get("pid") is None:
            try:
                _atomic_write_json(_audio_state_path(input_device), {**current, "pid": proc.pid, "timestamp": time.time()})
            except Exception as exc:
                self._log(input_device, f"Failed to record child pid on audio-state file: {exc}", force=True)
        # else: the child already published its own state (every
        # write_state() call in the generated script bakes in
        # process.pid() itself, see build_liquidsoap_script) -- nothing
        # to do here.

        self._procs[input_device] = proc
        self._scripts[input_device] = script_path
        self._current[input_device] = {"generation": generation, "pid": proc.pid, "launched_at": launched_at}
        self._stabilized[input_device] = False
        # Phase 3M: the exact encoder-like objects this launch
        # represents -- real Encoder rows or destinations_from_lkg_meta
        # stand-ins alike, whichever `encoders` the caller passed for
        # THIS launch. Single choke point: every _start_group call site
        # already receives exactly the right list for whatever it's
        # launching, so recording it generically here (rather than at
        # each of the several call sites) can't drift from what's
        # actually running. Used only for cross-group destination-
        # collision checks (_cross_group_destination_conflicts) --
        # never for qualification (_qualify_expected is separate and
        # narrower-scoped).
        self._running_encoders[input_device] = encoders
        self._write_group_state(input_device)
        names = ", ".join(e.name for e in encoders)
        self._log(input_device, f"Started (Liquidsoap pid={proc.pid}, generation={generation}) -> {names}", force=True)
        return True

    def _record_launch_failure(self, input_device, meta, exc, context):
        """Shared failure bookkeeping for every way _start_group can
        fail to produce a live child -- Popen() itself, or (Phase 2
        review-fix pass 2, Issue 3) an earlier filesystem write (the
        now-playing placeholder, the encoder script itself) or a
        malformed rollback script. Same event/counter/state-file
        machinery every time, so a filesystem failure is reported and
        retried exactly like every other launch failure already was --
        never a special, less-safe path. Always returns False, so call
        sites can `return self._record_launch_failure(...)` directly."""
        meta["consecutive_failures"] += 1
        meta["last_failure_message"] = f"{context}: {exc}"
        meta["last_exit_at"] = time.time()
        meta["last_exit_code"] = None
        self._current.pop(input_device, None)
        self._running_fingerprint.pop(input_device, None)
        self._running_encoders.pop(input_device, None)
        self._log(input_device, f"Failed to start: {context}: {exc} (failure #{meta['consecutive_failures']} in a row)", force=True)
        emit_event(
            category="encoder", level="error",
            title=f"Encoder group '{input_device}' failed to launch",
            detail={"input_device": input_device, "error": str(exc), "consecutive_failures": meta["consecutive_failures"]},
            dedupe_key=f"encoder|launch-failed|{input_device}",
        )
        self._write_group_state(input_device)
        # No child was ever spawned -- any "starting" placeholder
        # written moments ago (still pid=null) is now actively
        # misleading (it implies a launch is in flight when it has
        # already failed). Flip it to "dead" rather than leaving
        # "starting" to linger until the NEXT retry's own pre-spawn
        # write eventually overwrites it, possibly minutes from now
        # under backoff.
        self._mark_audio_state_dead(input_device)
        return False

    def _unlink_script_best_effort(self, input_device, script_path):
        """Best-effort removal of a no-longer-live child's script file
        -- shared by _handle_exit and _on_qualification_failed. Never
        raises (Phase 2 review-fix pass 2, Issue 3): a failure to
        delete a stale script file is, at worst, a small filesystem
        leak, never a reason to let an exception escape the supervisor
        mid-cleanup."""
        if not script_path:
            return
        try:
            script_path.unlink(missing_ok=True)
        except OSError as exc:
            self._log(input_device, f"Failed to remove stale script file {script_path}: {exc}")

    def _handle_exit(self, input_device, returncode):
        # Captured BEFORE any state is cleared below -- decides which
        # branch this exit takes (Phase 2J: a crash during candidate/
        # rollback PROBATION is a qualification failure, routed to
        # rollback/critical-stop, never the ordinary infra retry an
        # already-"accepted" child's crash gets).
        kind = self._launch_kind.get(input_device, "accepted")

        self._procs.pop(input_device, None)
        script_path = self._scripts.pop(input_device, None)
        self._unlink_script_best_effort(input_device, script_path)
        self._stabilized.pop(input_device, None)
        self._clear_qualification_tracking(input_device)

        meta = self._group_meta(input_device)
        meta["consecutive_failures"] += 1
        meta["last_exit_at"] = time.time()
        meta["last_exit_code"] = returncode
        meta["last_failure_message"] = f"Liquidsoap exited with code {returncode}"
        self._current.pop(input_device, None)  # no live child -- Phase 3 req #4: mark dead immediately
        self._running_fingerprint.pop(input_device, None)
        self._running_encoders.pop(input_device, None)

        self._mark_audio_state_dead(input_device)

        if kind == "candidate":
            self._log(input_device, f"Candidate Liquidsoap exited (code {returncode}) during probation -- treating as qualification failure.", force=True)
            self._reject_live_candidate(input_device, f"Liquidsoap exited with code {returncode} during probation")
            return
        if kind == "rollback":
            self._log(input_device, f"Rollback Liquidsoap exited (code {returncode}) during its own qualification.", force=True)
            self._on_rollback_failed(input_device, f"Liquidsoap exited with code {returncode} during rollback qualification")
            return

        # kind == "accepted" -- ordinary infrastructure retry, UNCHANGED
        # from pre-Phase-2 behavior (Phase 2O: this is a runtime/
        # infrastructure failure of an already-proven configuration,
        # not a configuration problem -- the existing bounded backoff
        # is exactly the right tool).
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

    # ------------------------------------------------------------------
    # Phase 2 hardening: candidate validation/preflight/qualification/
    # promotion/rollback. Every launch attempt (start(), and _check_
    # health's retry loop) goes through _launch_group instead of
    # calling _start_group directly -- _start_group itself is
    # unchanged (still the actual Popen()/state-tracking mechanics for
    # every launch, "accepted" or "candidate" or "rollback" alike).
    # ------------------------------------------------------------------

    def _clear_qualification_tracking(self, input_device):
        self._qualify_started_at.pop(input_device, None)
        self._qualify_generation.pop(input_device, None)
        self._qualify_deadline.pop(input_device, None)
        self._qualify_expected.pop(input_device, None)

    def _start_qualification_clock(self, input_device):
        """Arms qualification tracking for the group's CURRENT
        generation -- called immediately after a candidate or rollback
        launch succeeds. _qualify_started_at stays None until the
        first "ok" tick; only a CONTINUOUS run of "ok" ticks counts,
        so any interruption (a non-"ok" tick) resets it via
        _check_candidate_qualification, never accumulates across gaps."""
        current = self._current.get(input_device)
        if not current:
            return
        self._qualify_started_at[input_device] = None
        self._qualify_generation[input_device] = current["generation"]
        self._qualify_deadline[input_device] = time.monotonic() + CANDIDATE_QUALIFICATION_DEADLINE_SECONDS

    def _launch_group(self, input_device, encoders):
        """The single entry point every launch attempt goes through
        (replaces direct _start_group() calls in start() and _check_
        health()'s retry loop). Returns True/False exactly as
        _start_group does -- callers schedule a retry on False,
        unchanged contract.

        Decision:
          1. Current DB configuration's fingerprint matches the
             persisted LKG exactly -> launch directly via the plain,
             pre-Phase-2 _start_group() path. No candidate machinery,
             no added delay -- the common case (an ordinary restart of
             an unchanged, already-proven configuration).
          2. That fingerprint was already rejected (validation,
             preflight, or live qualification failure) and an LKG
             exists -> launch the LKG's own persisted script instead
             of re-attempting the known-bad configuration
             automatically (Phase 2K: never silently retry a rejected
             candidate forever).
          3. Manager is in critical-stopped state for this group (both
             a candidate AND its rollback already failed) AND the
             desired fingerprint has ALREADY been rejected -> LKG-only
             relaunches via the ordinary retry/backoff loop (infra
             recovery only). A critical-stopped group whose desired
             fingerprint is genuinely NEW (never attempted) still gets
             a fresh candidate attempt below (Phase 3 fix -- see this
             module's Phase 3 docstring addendum: under Phase 2 this
             was unreachable in practice, since the only way to clear
             critical_stopped was a full process restart, which always
             cleared it fresh; Phase 3 reconciliation can keep a
             process alive for a long time across many DB edits while
             critical_stopped, so "save a corrected configuration to
             try again" -- the admin's own existing promise -- must
             actually be true).
          4. Otherwise -> full candidate pipeline: validate, render,
             preflight (never touching the active/LKG paths or
             opening the live ALSA device), and only then launch on
             probation for live qualification."""
        from . import lkg as lkg_module

        close_old_connections()
        slug = _slug(input_device)
        desired_fp = lkg_module.compute_fingerprint(input_device, encoders)
        self._desired_fingerprint[input_device] = desired_fp
        try:
            lkg_script, lkg_meta = lkg_module.read_lkg(slug)
        except OSError as exc:
            # Phase 2 review-fix pass 2, Issue 3: never let a
            # filesystem failure escape and crash the supervisor.
            # Degrades to "treat as if no LKG exists" -- worse
            # (a needless full candidate re-validation instead of the
            # fast path) but never unsafe: the full candidate pipeline
            # below still validates/preflights/qualifies before
            # anything is trusted.
            self._log(input_device, f"Failed to read last-known-good state ({exc}) -- treating as bootstrap.", force=True)
            emit_event(
                category="encoder", level="error",
                title=f"Encoder group '{input_device}' failed to read last-known-good state",
                detail={"input_device": input_device, "error": str(exc)},
                dedupe_key=f"encoder|lkg-read-failed|{input_device}",
            )
            lkg_script, lkg_meta = None, None
        lkg_fp = (lkg_meta or {}).get("fingerprint")
        # Read-through cache (see __init__) -- this process is the only
        # LKG writer, so this is always ground truth, no extra I/O for
        # _write_group_state's heartbeat to pay later.
        self._accepted_fingerprint[input_device] = lkg_fp
        rejected = self._rejected_fingerprints.get(input_device, set())

        if lkg_script is not None and desired_fp == lkg_fp:
            self._launch_kind[input_device] = "accepted"
            self._clear_qualification_tracking(input_device)
            ok = self._start_group(input_device, encoders)
            if ok:
                self._running_fingerprint[input_device] = desired_fp
            return ok

        if lkg_script is not None and desired_fp in rejected:
            self._log(input_device, f"Desired configuration (fingerprint {desired_fp[:12]}) was previously rejected -- launching last-known-good instead.", force=True)
            self._launch_kind[input_device] = "accepted"
            self._clear_qualification_tracking(input_device)
            # Phase 3 review-fix pass, Issue 1: `encoders` here is the
            # REJECTED desired rows, not what's actually about to run
            # (the LKG script) -- record the LKG's own frozen
            # destination identity instead, exactly like
            # _start_rollback and _fallback_after_rejected_prelaunch_
            # candidate already do, so a rejected/differing DB edit
            # can never masquerade as this group's real running
            # identity for Phase 3M cross-group collision purposes.
            lkg_expected = lkg_module.destinations_from_lkg_meta(lkg_meta)
            ok = self._start_group(input_device, lkg_expected, script_override=lkg_script)
            if ok:
                self._running_fingerprint[input_device] = lkg_fp
            return ok

        if lkg_script is None and self._critical_stopped.get(input_device):
            # Nothing safe to launch and no further automatic
            # switching allowed -- caller's own retry/backoff is what
            # keeps trying (matches bootstrap-with-no-LKG behavior).
            return False

        # Full candidate pipeline -- reached for a genuinely new (never
        # rejected) desired fingerprint, whether or not this group is
        # critical-stopped from an earlier, unrelated incident.
        ok, candidate_script, failure_kind, reason, detail = self._static_check_candidate(input_device, encoders)
        if not ok:
            self._reject_prelaunch_candidate(input_device, encoders, desired_fp, failure_kind, reason, detail=detail)
            return self._fallback_after_rejected_prelaunch_candidate(input_device, encoders, lkg_script, lkg_meta)

        if self._cross_group_collision_blocked(input_device, encoders, desired_fp):
            # Deliberately NOT added to _rejected_fingerprints -- see
            # _cross_group_collision_blocked's own comment: re-evaluated
            # fresh every attempt so it self-heals once the other
            # group relinquishes the destination, unlike a genuine
            # validation/preflight failure.
            return self._fallback_after_rejected_prelaunch_candidate(input_device, encoders, lkg_script, lkg_meta)

        # Every static check passed -- launch for real, on probation,
        # the EXACT script text that was just checked (never re-render
        # after the fact -- Phase 3E).
        ok = self._start_group(input_device, encoders, script_override=candidate_script)
        if ok:
            self._launch_kind[input_device] = "candidate"
            self._candidate_fingerprint[input_device] = desired_fp
            self._candidate_encoders[input_device] = encoders
            # For "candidate" (unlike "rollback"), the desired DB
            # configuration IS what was just launched -- these real,
            # just-validated/preflighted Encoder rows are exactly what
            # qualification should check.
            self._qualify_expected[input_device] = encoders
            self._running_fingerprint[input_device] = desired_fp
            self._start_qualification_clock(input_device)
            # _start_group's own internal state write happened a moment
            # ago with launch_kind still defaulting to "accepted" (it
            # runs before this line) -- refresh immediately so the
            # on-disk state (admin's only view into this) never shows a
            # stale "accepted" for what's actually an unqualified
            # candidate, even for the brief window before the next
            # regular health-tick write.
            self._write_group_state(input_device)
            self._log(input_device, f"Candidate launched (fingerprint={desired_fp[:12]}), qualification in progress.", force=True)
            emit_event(
                category="encoder", level="info",
                title=f"Encoder group '{input_device}' candidate launched",
                detail={
                    "input_device": input_device, "fingerprint": desired_fp,
                    "generation": self._current[input_device]["generation"],
                    "encoder_names": [e.name for e in encoders],
                },
                dedupe_key=f"encoder|candidate-launched|{input_device}",
            )
        return ok

    def _static_check_candidate(self, input_device, encoders, generation_label="preflight-check"):
        """Validate -> render -> write to CANDIDATE_DIR -> liquidsoap
        --check, the exact sequence a configuration must pass before it
        may ever be launched -- shared by cold-bootstrap (_launch_group,
        above) and Phase 3 reconciliation (_reconcile_changed_group,
        below) so there is exactly ONE implementation of this sequence,
        never two that could silently disagree. Never touches the
        live/active script path or opens the ALSA device -- see
        preflight.py's own module docstring for why `liquidsoap --check`
        is safe to run this way even while a healthy child holds the
        real device.

        Returns (ok, candidate_script_or_None, failure_kind_or_None,
        reason_or_None, detail_dict). `failure_kind` is "validation" or
        "preflight", matching _reject_prelaunch_candidate's own
        vocabulary. `candidate_script` is the EXACT text that passed --
        a caller that goes on to launch it must launch this exact
        string (via _start_group's script_override), never re-render,
        so the configuration that was checked is provably the
        configuration that runs (Phase 3E)."""
        from . import lkg as lkg_module
        from . import preflight as preflight_module
        from .validation import validate_full_configuration

        errors = validate_full_configuration(encoders)
        if errors:
            return False, None, "validation", "; ".join(errors), {}

        slug = _slug(input_device)
        host_aircheck = input_device == DEFAULT_INPUT_DEVICE
        script = build_liquidsoap_script(input_device, encoders, host_aircheck=host_aircheck, generation=generation_label)
        try:
            candidate_path = lkg_module.write_candidate(slug, script)
        except OSError as exc:
            # Same failure class as an ordinary preflight rejection --
            # the candidate could not even be rendered to disk to be
            # checked. Never let this propagate and crash the
            # supervisor (Phase 2 review-fix pass 2, Issue 3).
            return False, None, "preflight", f"failed to write candidate script: {exc}", {}
        try:
            result = preflight_module.run_preflight(candidate_path, encoders)
        finally:
            lkg_module.cleanup_candidate(candidate_path)

        if not result.ok:
            return False, None, "preflight", result.reason, result.detail
        return True, script, None, None, {}

    def _reject_prelaunch_candidate(self, input_device, encoders, fingerprint, failure_kind, reason, detail=None):
        """A candidate failed validation or preflight -- BEFORE ever
        being Popen()'d. Nothing was launched, nothing running was
        touched; this only records the rejection and emits an event.
        failure_kind is "validation" or "preflight" (Phase 2O
        classification: both are configuration/static failures, never
        entered into the runtime retry/backoff loop as themselves --
        see _fallback_after_rejected_prelaunch_candidate)."""
        self._rejected_fingerprints.setdefault(input_device, set()).add(fingerprint)
        self._log(input_device, f"Candidate {failure_kind} failed: {reason}", force=True)
        emit_event(
            category="encoder", level="error",
            title=f"Encoder group '{input_device}' candidate {failure_kind} failed",
            detail={
                "input_device": input_device, "fingerprint": fingerprint,
                "reason": reason, "encoder_names": [e.name for e in encoders],
                **(detail or {}),
            },
            dedupe_key=f"encoder|candidate-{failure_kind}-failed|{input_device}",
        )

    def _fallback_after_rejected_prelaunch_candidate(self, input_device, encoders, lkg_script, lkg_meta=None):
        """After a pre-launch rejection: if an LKG exists, launch it
        directly (trusted immediately, no re-qualification -- it's
        already-proven content, not a new configuration) so the group
        still ends up running SOMETHING rather than sitting idle over
        a config error elsewhere. If no LKG exists at all (bootstrap),
        there is nothing safe to launch; the caller's own retry/
        backoff is what keeps re-checking (each retry re-reads current
        DB state fresh, so an operator's fix is picked up automatically
        without needing a separate no-retry mechanism).

        Phase 3 review-fix pass, Issue 1: `encoders` here is the
        REJECTED candidate's own rows -- exactly the values Issue 1
        found being wrongly recorded as "what's actually running"
        (self._running_encoders, used for Phase 3M cross-group
        collision checks) when the LKG script itself is what's
        actually launched. Reconstructs the LKG's own frozen
        destination identity from `lkg_meta` (same
        destinations_from_lkg_meta helper _start_rollback already
        uses, for the same reason) and records THAT, never the
        rejected `encoders`, as this group's running identity."""
        if lkg_script is not None:
            from . import lkg as lkg_module

            self._log(input_device, "Launching last-known-good instead.", force=True)
            self._launch_kind[input_device] = "accepted"
            self._clear_qualification_tracking(input_device)
            lkg_expected = lkg_module.destinations_from_lkg_meta(lkg_meta)
            ok = self._start_group(input_device, lkg_expected, script_override=lkg_script)
            if ok:
                self._running_fingerprint[input_device] = (lkg_meta or {}).get("fingerprint")
            return ok
        return False

    def _reject_live_candidate(self, input_device, reason):
        """A candidate that WAS successfully launched failed to
        qualify (crashed during probation, or its deadline expired --
        see _handle_exit and _on_qualification_failed). Marks the
        fingerprint rejected and attempts rollback."""
        fingerprint = self._candidate_fingerprint.get(input_device)
        encoders = self._candidate_encoders.get(input_device, [])
        if fingerprint is not None:
            self._rejected_fingerprints.setdefault(input_device, set()).add(fingerprint)
        emit_event(
            category="encoder", level="error",
            title=f"Encoder group '{input_device}' candidate qualification failed",
            detail={
                "input_device": input_device, "fingerprint": fingerprint,
                "reason": reason, "encoder_names": [e.name for e in encoders],
            },
            dedupe_key=f"encoder|candidate-qualification-failed|{input_device}",
        )
        if self._reconcile_origin.get(input_device):
            # Peek only (do not pop) -- _start_rollback below still
            # needs to see this flag to correctly attribute whatever it
            # decides next (rollback_started, or rollback_failed if
            # there's no usable LKG at all).
            self._record_reconcile_outcome(input_device, "candidate_failed", reason)
        self._start_rollback(input_device, encoders, reason)

    def _start_rollback(self, input_device, encoders, candidate_failure_reason):
        """Rollback is always a FRESH launch of the persisted LKG
        script (never a resumption of anything) -- see lkg.py's own
        module docstring for why the LKG must be self-contained. If no
        LKG exists at all, there is structurally nothing to roll back
        to (Phase 2L bootstrap case); falls through to the existing
        bounded retry/backoff, reported critical, with no invented
        rollback target. A filesystem failure while reading the LKG
        (Phase 2 review-fix pass 2, Issue 3) is treated identically --
        there is equally nothing safe to relaunch either way.

        `encoders` here is the REJECTED CANDIDATE's own list (passed
        through from _reject_live_candidate) -- used ONLY as a log-line
        fallback below if the LKG's own metadata predates
        lkg.destinations_from_lkg_meta (see that function). It must
        NEVER be used to decide what the rollback's OWN health
        qualification checks -- see _qualify_expected's docstring in
        __init__ for why (post-review fix: an earlier draft did exactly
        this and was a real, live bug -- rollback health was evaluated
        against the rejected candidate's SID/host/port instead of what
        was actually relaunched)."""
        from . import lkg as lkg_module

        slug = _slug(input_device)
        try:
            lkg_script, lkg_meta = lkg_module.read_lkg(slug)
        except OSError as exc:
            self._log(input_device, f"Failed to read last-known-good state for rollback: {exc}", force=True)
            lkg_script, lkg_meta = None, None
            lkg_problem = f"last-known-good state could not be read: {exc}"
        else:
            lkg_problem = lkg_module.describe_lkg_problem(slug) if lkg_script is None else ""

        if lkg_script is None:
            reason = lkg_problem or "no last-known-good configuration exists to roll back to"
            self._log(input_device, f"Candidate rejected and {reason}.", force=True)
            emit_event(
                category="encoder", level="critical",
                title=f"Encoder group '{input_device}' has no usable last-known-good configuration",
                detail={"input_device": input_device, "reason": candidate_failure_reason, "lkg_problem": reason},
                dedupe_key=f"encoder|no-lkg-for-rollback|{input_device}",
            )
            self._launch_kind[input_device] = "accepted"
            if self._reconcile_origin.pop(input_device, False):
                # Terminal for this reconciliation attempt -- there is
                # nothing left to roll back to, so pop rather than peek.
                self._record_reconcile_outcome(input_device, "rollback_failed", reason)
            self._schedule_retry(input_device)
            return

        # The LKG's OWN destination identity, reconstructed from its
        # frozen metadata -- NEVER re-derived from the current DB (see
        # lkg.destinations_from_lkg_meta's own docstring for why). This
        # is what qualification must check, and (falling back to the
        # candidate's names only for a legacy/metadata-less LKG, purely
        # cosmetic) what the launch log line describes as running.
        lkg_expected = lkg_module.destinations_from_lkg_meta(lkg_meta)

        emit_event(
            category="encoder", level="warning",
            title=f"Encoder group '{input_device}' rolling back to last-known-good",
            detail={
                "input_device": input_device, "candidate_failure_reason": candidate_failure_reason,
                "rollback_fingerprint": (lkg_meta or {}).get("fingerprint"),
            },
            dedupe_key=f"encoder|rollback-started|{input_device}",
        )
        ok = self._start_group(input_device, lkg_expected or encoders, script_override=lkg_script)
        if not ok:
            # _start_group already recorded its own failure bookkeeping/
            # event for the launch failure itself -- this additionally
            # treats it as a rollback failure (critical, ordinary
            # backoff from here) rather than silently stopping.
            self._on_rollback_failed(input_device, "failed to launch rollback process")
            return
        self._running_fingerprint[input_device] = (lkg_meta or {}).get("fingerprint")
        self._accepted_fingerprint[input_device] = (lkg_meta or {}).get("fingerprint")
        self._launch_kind[input_device] = "rollback"
        # Deliberately lkg_expected here, WITHOUT the `or encoders`
        # fallback used for the log line above: an empty list makes
        # evaluate_encoder_group_health report "unknown" (never a false
        # "ok") until the qualification deadline expires -- the correct,
        # conservative outcome when a legacy/corrupt LKG has no
        # reconstructable destinations, rather than silently checking
        # the wrong (candidate's) SIDs.
        self._qualify_expected[input_device] = lkg_expected
        self._start_qualification_clock(input_device)
        if self._reconcile_origin.get(input_device):
            # Peek only -- not terminal yet, rollback still has to
            # qualify. _on_rollback_succeeded/_on_rollback_failed below
            # will pop this flag when this attempt actually concludes.
            self._record_reconcile_outcome(input_device, "rollback_started")
        self._write_group_state(input_device)  # same reasoning as the candidate-launch site above

    def _on_rollback_failed(self, input_device, reason, health_detail=None):
        """Rollback itself could not establish health -- Phase 2K's
        core distinction: this protects against a bad CONFIGURATION
        change, it cannot repair broken infrastructure (dead ALSA
        device, unreachable network, a down Shoutcast server). Stops
        further automatic candidate<->LKG switching for this group
        (no bounce loop) and hands off to the existing bounded retry/
        backoff for whatever infrastructure recovery is possible."""
        self._critical_stopped[input_device] = True
        self._launch_kind[input_device] = "accepted"
        if self._reconcile_origin.pop(input_device, False):
            self._record_reconcile_outcome(input_device, "rollback_failed", reason)
        self._log(input_device, f"Rollback failed: {reason}. Automatic configuration switching stopped -- infrastructure retry only.", force=True)
        emit_event(
            category="encoder", level="critical",
            title=f"Encoder group '{input_device}' rollback failed -- last-known-good cannot establish health",
            detail={"input_device": input_device, "reason": reason, **(health_detail or {})},
            dedupe_key=f"encoder|rollback-failed|{input_device}",
        )
        self._schedule_retry(input_device)

    def _on_rollback_succeeded(self, input_device, health_detail):
        self._launch_kind[input_device] = "accepted"
        self._clear_qualification_tracking(input_device)
        self._retry_index[input_device] = 0
        meta = self._group_meta(input_device)
        meta["consecutive_failures"] = 0
        if self._reconcile_origin.pop(input_device, False):
            self._record_reconcile_outcome(input_device, "rollback_succeeded")
        self._write_group_state(input_device)  # launch_kind just changed -- refresh the admin-visible snapshot
        self._log(input_device, "Rollback qualified -- last-known-good restored and healthy.", force=True)
        emit_event(
            category="encoder", level="info",
            title=f"Encoder group '{input_device}' rollback succeeded",
            detail={"input_device": input_device},
            dedupe_key=f"encoder|rollback-succeeded|{input_device}",
        )

    def _promote_candidate(self, input_device, health_detail):
        """Candidate has been continuously healthy for
        CANDIDATE_QUALIFICATION_SECONDS -- persist it as the new LKG.
        Reads the ACTUAL script text that's actually running (the
        active script path, unchanged since this generation launched)
        rather than re-rendering, so the LKG is guaranteed to be
        byte-identical to what was actually proven, not a fresh
        render that could theoretically differ.

        Phase 2 review-fix pass 2, Issue 3: writing the LKG can fail
        (ENOSPC, EACCES, EROFS, a genuine I/O error, ...). The
        candidate at this point is proven-healthy, running, and
        carrying real audio -- a persistence failure must NEVER stop
        it, and must NEVER be allowed to propagate out of this method
        (the caller is _check_candidate_qualification, itself called
        from the supervisor's own main loop with nothing above it to
        catch an escaping exception -- see encoder_manager.py's own
        module docstring addendum). On failure: the candidate is left
        exactly as it is (launch_kind stays "candidate", qualification
        tracking is deliberately NOT cleared) -- which means the very
        next health tick, seeing the SAME already-elapsed qualified
        duration, retries this exact promotion automatically, with no
        new state machinery needed. The OLD LKG (if any) is completely
        untouched, and the candidate is explicitly NOT considered
        durably accepted until a promotion actually succeeds."""
        from . import lkg as lkg_module

        slug = _slug(input_device)
        current = self._current.get(input_device, {})
        encoders = self._candidate_encoders.get(input_device, [])
        fingerprint = self._candidate_fingerprint.get(input_device)
        script_path = self._scripts.get(input_device)
        script_text = script_path.read_text(encoding="utf-8") if script_path and script_path.is_file() else None
        if script_text is None:
            self._log(input_device, "Cannot promote: active script file is missing.", force=True)
            return

        meta = {
            "fingerprint": fingerprint,
            "accepted_at": time.time(),
            "input_device": input_device,
            "encoder_ids": [e.id for e in encoders],
            "encoder_names": [e.name for e in encoders],
            "generation": current.get("generation"),
            "qualification_duration_seconds": CANDIDATE_QUALIFICATION_SECONDS,
            # Frozen destination identity (host/port/SID) for THIS
            # proven-healthy set -- consumed by
            # lkg.destinations_from_lkg_meta at a future rollback (or
            # by the ordinary dashboard probe, via lkg.resolve_
            # expected_destinations), so that health-checking always
            # asks about what was ACTUALLY relaunched rather than
            # whatever the DB currently holds (which may, by then, be
            # a rejected edit of these very rows). shoutcast_sid uses
            # the lenient property (same one evaluate_encoder_group_
            # health itself reads), computed once now from rows just
            # proven to work rather than recomputed later from rows
            # that might not be. `protocol`/`mount` (Phase 3M) are the
            # two additional fields encoders.services.validation.
            # normalized_destination_key needs beyond host/port/SID --
            # without them an Icecast destination's stand-in can't be
            # told apart from a different mount on the SAME host:port,
            # so a group running via LKG fallback/rollback couldn't be
            # correctly checked for a cross-group collision. A legacy
            # LKG promoted before this field existed simply won't have
            # them (destinations_from_lkg_meta below degrades to None,
            # which normalized_destination_key already treats as
            # "can't normalize, no collision key" -- never a false
            # match) until its next promotion.
            "destinations": [
                {
                    "encoder_id": e.id, "name": e.name, "host": e.host, "port": e.port,
                    "shoutcast_sid": e.shoutcast_sid, "protocol": e.protocol, "mount": e.mount,
                    # Roadmap 3.10: frozen alongside protocol/mount for the
                    # exact same reason those two were added (Phase 3M) --
                    # rollback/monitoring health-checking needs to know a
                    # reconstructed destination's PROVIDER too, e.g. so a
                    # "shoutcast1" stand-in that's actually Radio.co is
                    # never routed through the external Shoutcast DNAS
                    # /statistics probe (see monitoring/services/probes.py's
                    # _uses_shoutcast_dnas_probe). getattr with a "generic"
                    # default: `e` here is always a real Encoder row (this
                    # dict is built fresh at promotion time from
                    # self._candidate_encoders, never from LKG stand-ins),
                    # so the field is always present in practice -- the
                    # default only guards a stray non-Encoder object ever
                    # reaching this code path.
                    "provider": getattr(e, "provider", "generic"),
                }
                for e in encoders
            ],
        }
        try:
            lkg_module.write_lkg(slug, script_text, meta)
        except OSError as exc:
            # NOT force=True: this can legitimately retry every health
            # tick if persistence stays broken -- _log's own 30s
            # identical-message throttle keeps the journal sane; emit_
            # event's existing dedupe_key coalescing does the same for
            # the dashboard-facing record. Deliberately does NOT touch
            # launch_kind/qualification tracking/rejected_fingerprints/
            # critical_stopped -- the candidate stays exactly as it was,
            # already healthy, simply not yet durably accepted.
            self._log(input_device, f"Candidate qualified but last-known-good persistence FAILED ({exc}) -- leaving candidate running, will retry.")
            emit_event(
                category="encoder", level="critical",
                title=f"Encoder group '{input_device}' qualified but could not persist last-known-good",
                detail={"input_device": input_device, "fingerprint": fingerprint, "error": str(exc)},
                dedupe_key=f"encoder|promotion-persist-failed|{input_device}",
            )
            return
        self._rejected_fingerprints.pop(input_device, None)
        self._critical_stopped.pop(input_device, None)
        self._launch_kind[input_device] = "accepted"
        self._accepted_fingerprint[input_device] = fingerprint
        self._clear_qualification_tracking(input_device)
        if self._reconcile_origin.pop(input_device, False):
            # This promotion concludes a candidate launched by Phase 3
            # reconciliation (_reconcile_changed_group), not an ordinary
            # Phase 2 cold-boot/retry promotion -- record the eventual
            # outcome. _write_group_state below picks up last_reconcile_*
            # in the same write as launch_kind.
            self._record_reconcile_outcome(input_device, "candidate_promoted")
        self._write_group_state(input_device)  # launch_kind just changed -- refresh the admin-visible snapshot
        self._log(input_device, f"Candidate qualified and promoted to last-known-good (generation={current.get('generation')}).", force=True)
        emit_event(
            category="encoder", level="info",
            title="Encoder configuration qualified and promoted to last-known-good",
            detail={
                "input_device": input_device, "generation": current.get("generation"),
                "fingerprint": fingerprint, "encoder_names": [e.name for e in encoders],
                "qualification_duration_seconds": CANDIDATE_QUALIFICATION_SECONDS,
            },
            dedupe_key=f"encoder|promoted|{input_device}",
        )

    def _on_qualification_failed(self, input_device, kind, reason, health_detail):
        """A STILL-RUNNING candidate/rollback exceeded its deadline
        without reaching sustained health (as opposed to _handle_exit's
        crash path, which fires for a process that already exited on
        its own). Terminates the not-yet-trustworthy child first, then
        routes exactly like a crash during probation would."""
        proc = self._procs.get(input_device)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError) as exc:
                    self._log(input_device, f"Process already gone while killing candidate/rollback child: {exc}")
            except (ProcessLookupError, OSError) as exc:
                # A narrow, real race: the child could have already
                # exited (and been reaped, e.g. by _check_health's own
                # poll() loop) between being read into `proc` above and
                # this terminate() call. Nothing left to signal --
                # continue with cleanup exactly as if termination
                # succeeded (Phase 2 review-fix pass 2, Issue 3: never
                # let this escape and crash the supervisor).
                self._log(input_device, f"Process already gone while terminating candidate/rollback child: {exc}")
        self._procs.pop(input_device, None)
        script_path = self._scripts.pop(input_device, None)
        self._unlink_script_best_effort(input_device, script_path)
        self._stabilized.pop(input_device, None)
        self._current.pop(input_device, None)
        self._running_fingerprint.pop(input_device, None)
        self._running_encoders.pop(input_device, None)
        self._mark_audio_state_dead(input_device)
        self._clear_qualification_tracking(input_device)

        if kind == "candidate":
            self._reject_live_candidate(input_device, reason)
        else:
            self._on_rollback_failed(input_device, reason, health_detail)

    def _check_candidate_qualification(self, input_device):
        """Runs every health tick for a group currently on probation
        (launch_kind "candidate" or "rollback"); a no-op for
        "accepted". Reuses evaluate_encoder_group_health -- the EXACT
        same aggregate the dashboard/monitoring probe uses (Phase 2H:
        never a parallel health implementation that could disagree
        with it) -- tracking how long it has continuously reported
        "ok" for the CURRENT generation. Promotes on success; a crash
        is handled by _handle_exit (which runs earlier in the same
        _check_health tick), so this function only ever judges a
        STILL-RUNNING candidate's deadline."""
        kind = self._launch_kind.get(input_device)
        if kind not in ("candidate", "rollback"):
            return
        current = self._current.get(input_device)
        if not current:
            return
        generation = current["generation"]
        if self._qualify_generation.get(input_device) != generation:
            return  # defensive -- should always match if _start_qualification_clock ran

        # Local import: monitoring/services/probes.py imports FROM this
        # module (DEFAULT_INPUT_DEVICE, STABILIZATION_SECONDS, etc.) --
        # a module-level import here would be circular.
        from monitoring.services.probes import evaluate_encoder_group_health

        close_old_connections()
        slug = _slug(input_device)
        # _qualify_expected, NOT _candidate_encoders: for "rollback"
        # these differ on purpose (see _qualify_expected's docstring in
        # __init__) -- this must always ask about what was ACTUALLY
        # relaunched (the candidate's own rows, or the LKG's frozen
        # snapshot), never the live DB's current (possibly still-
        # rejected) configuration.
        encoders = self._qualify_expected.get(input_device, [])
        status, detail = evaluate_encoder_group_health(slug, encoders, ENCODER_MANAGER_SYSTEMD_UNIT)

        now_mono = time.monotonic()
        deadline = self._qualify_deadline.get(input_device)

        if status != "ok":
            self._qualify_started_at[input_device] = None
            if deadline is not None and now_mono > deadline:
                self._on_qualification_failed(
                    input_device, kind,
                    f"qualification deadline expired (last status: {status} -- {detail.get('reason', '')})",
                    detail,
                )
            return

        if self._qualify_started_at.get(input_device) is None:
            self._qualify_started_at[input_device] = now_mono
            return

        elapsed = now_mono - self._qualify_started_at[input_device]
        if elapsed < CANDIDATE_QUALIFICATION_SECONDS:
            if deadline is not None and now_mono > deadline:
                self._on_qualification_failed(input_device, kind, "qualification deadline expired before sustained health was reached", detail)
            return

        if kind == "candidate":
            self._promote_candidate(input_device, detail)
        else:
            self._on_rollback_succeeded(input_device, detail)

    # ------------------------------------------------------------------
    # Phase 3 hardening: per-input-device reconciliation. Replaces the
    # Django admin's whole-service `systemctl restart isadoraair-
    # encoders` for routine Encoder edits -- this manager discovers DB
    # drift itself, on its own existing 5s health-tick cadence (see
    # _check_health), and reconciles ONLY the input-device group(s)
    # that actually changed. Every actual launch/promotion/rollback
    # still goes through the exact same Phase 2 primitives above
    # (_launch_group, _start_group, _start_rollback,
    # _check_candidate_qualification, _promote_candidate) -- nothing
    # here duplicates that machinery, it only decides WHEN to invoke it
    # for an already-running group, which Phase 2 never needed to do.
    # ------------------------------------------------------------------

    def _record_reconcile_outcome(self, input_device, result, error=""):
        """Phase 3O: the eventual-apply-outcome trio surfaced in
        group-state JSON (last_reconcile_result/_error/_at). `result`
        is a short machine-readable label (e.g. "candidate_launched",
        "static_validation_rejected", "blocked_cross_group_collision",
        "removed") -- NOT a duplicate of the SystemEvent stream (see
        each call site's own emit_event, where one exists); this is
        purely the compact, always-current snapshot admin/monitoring
        read without having to replay events.

        Phase 3 review-fix pass, Issue 2 (found while adding the
        desired-vs-desired collision check): always persists via
        _write_group_state -- most call sites already have a live
        child whose OWN heartbeat would eventually write this same
        tick anyway, but a "new" group blocked before it EVER
        launches (no live child, ever, for either the running-vs-
        desired or desired-vs-desired collision checks) has no other
        write path at all, and would otherwise stay invisible to
        admin/monitoring (a separate process, on-disk state only)
        despite genuinely being blocked."""
        meta = self._group_meta(input_device)
        meta["last_reconcile_result"] = result
        meta["last_reconcile_error"] = error or ""
        meta["last_reconcile_at"] = time.time()
        self._write_group_state(input_device)

    def _cross_group_destination_conflicts(self, input_device, encoders):
        """Phase 3M: does `encoders` (a candidate about to be checked/
        launched for `input_device`) want any real destination that
        ANOTHER group is currently, actually running? Reuses
        encoders.services.validation.normalized_destination_key --
        the SAME protocol-aware (Icecast mount / Shoutcast1 host:port /
        Shoutcast2 host:port:SID) identity validate_group() already
        uses for WITHIN-group duplicate checking -- rather than a
        second, parallel notion of "same destination."

        Checked only against self._running_encoders -- what's
        genuinely live right now, the real collision risk (two
        processes racing for one ALSA/network/telnet resource) -- not
        against every other group's LKG regardless of whether it's
        currently running; a group with no live child holds nothing to
        collide with. Returns a list of (destination_key, other_
        input_device, other_encoder_name) tuples, empty if clear."""
        from .validation import normalized_destination_key

        desired_keys = {}
        for e in encoders:
            key = normalized_destination_key(e)
            if key is not None:
                desired_keys.setdefault(key, e)

        conflicts = []
        for other_device, other_encoders in self._running_encoders.items():
            if other_device == input_device:
                continue
            for e in other_encoders:
                key = normalized_destination_key(e)
                if key in desired_keys:
                    conflicts.append((key, other_device, getattr(e, "name", "?")))
        return conflicts

    def _cross_group_collision_blocked(self, input_device, encoders, desired_fp):
        """Checks _cross_group_destination_conflicts and, if any exist,
        reports them (log, emit_event, _record_reconcile_outcome) in
        exactly one place -- reused by every call site that must
        refuse to launch a colliding configuration: cold-bootstrap/
        retry (_launch_group), and reconciliation's own pre-dispatch
        filter and per-group replacement path (_reconcile_inner,
        _reconcile_changed_group). Returns True if blocked (nothing
        should be launched), False if clear.

        Deliberately NEVER added to _rejected_fingerprints -- re-
        evaluated fresh on every attempt so it self-heals the moment
        the other group relinquishes the destination, unlike a genuine
        validation/preflight failure (which IS sticky -- see
        _reject_prelaunch_candidate)."""
        conflicts = self._cross_group_destination_conflicts(input_device, encoders)
        if not conflicts:
            return False
        reason = "; ".join(f"{key} already in use by group {other!r} ({name!r})" for key, other, name in conflicts)
        self._log(input_device, f"Blocked by cross-group destination collision: {reason}", force=True)
        emit_event(
            category="encoder", level="warning",
            title=f"Encoder group '{input_device}' configuration blocked by cross-group destination collision",
            detail={
                "input_device": input_device, "fingerprint": desired_fp,
                "conflicts": [{"destination": key, "other_group": other, "other_encoder": name} for key, other, name in conflicts],
            },
            dedupe_key=f"encoder|cross-group-collision|{input_device}",
        )
        self._record_reconcile_outcome(input_device, "blocked_cross_group_collision", reason)
        return True

    def _desired_vs_desired_conflicts(self, desired_groups):
        """Phase 3 review-fix pass, Issue 2: does any pair of DESIRED
        groups (no running/live state involved at all -- purely the
        current DB read) claim the same normalized destination? This
        is an intrinsically ambiguous topology, not something
        reconciliation order should ever get to silently resolve by
        picking whichever one happens to launch first.

        Returns {input_device: {other_device, ...}} for every device
        involved in at least one such conflict -- empty dict if none.
        Deliberately does NOT consult self._running_encoders/
        _running_fingerprint at all: a legitimate input-device move
        (Encoder.input_device changing) can only ever leave the moved
        row in ONE group's desired set at a time, so it structurally
        cannot produce a desired-vs-desired overlap -- only two
        genuinely independent rows configured with the same
        destination can. Reuses validation.normalized_destination_key
        -- the identical protocol-aware identity every other collision
        check in this file already uses, never a second notion of
        "same destination"."""
        from .validation import normalized_destination_key

        keys_by_device = {}
        for device, encoders in desired_groups.items():
            keys = set()
            for e in encoders:
                key = normalized_destination_key(e)
                if key is not None:
                    keys.add(key)
            keys_by_device[device] = keys

        conflicts = {}
        devices = list(desired_groups)
        for i, a in enumerate(devices):
            for b in devices[i + 1:]:
                if keys_by_device[a] & keys_by_device[b]:
                    conflicts.setdefault(a, set()).add(b)
                    conflicts.setdefault(b, set()).add(a)
        return conflicts

    def _report_desired_conflict(self, input_device, other_devices):
        """Reports (log, emit_event, _record_reconcile_outcome) an
        Issue-2-style desired-vs-desired collision for ONE side of it
        -- called once per involved device (see _reconcile_inner) so
        every group sharing the ambiguous destination is independently
        visible in its own group-state/events, regardless of which
        side an operator happens to be looking at."""
        others = ", ".join(sorted(other_devices))
        reason = f"desired configuration also claims a destination desired by: {others}"
        self._log(input_device, f"Blocked by desired-vs-desired destination collision: {reason}", force=True)
        emit_event(
            category="encoder", level="warning",
            title=f"Encoder group '{input_device}' desired configuration conflicts with another group's desired configuration",
            detail={"input_device": input_device, "conflicting_groups": sorted(other_devices)},
            dedupe_key=f"encoder|desired-collision|{input_device}",
        )
        self._record_reconcile_outcome(input_device, "blocked_desired_collision", reason)

    def _stop_group_intentionally(self, input_device, reason):
        """Phase 3F: safely stop a group's CURRENT live child as a
        deliberate act (about to be replaced or removed) -- distinct
        from _handle_exit's crash path in every way that matters:
        never increments consecutive_failures, never records a
        last_exit_code/last_failure_message as if something went
        wrong, never schedules ordinary retry/backoff, never emits a
        child-crash-shaped event. Only ever touches `input_device`'s
        own bookkeeping.

        Bounded terminate -> wait -> kill -> wait, exactly like
        _on_qualification_failed's existing termination sequence, plus
        one more confirmation: if the process STILL can't be confirmed
        gone after SIGKILL, this refuses to proceed (returns False)
        rather than risk a second process racing the first for the
        same ALSA device, Shoutcast destination, or aircheck telnet
        port (Phase 3F's hard requirement). That is a deliberately
        conservative, rare-case failure mode -- NOTHING is cleared in
        that case (self._current/_procs/_running_fingerprint/etc. are
        left exactly as they were, still describing the presumed-
        still-alive old child, since that is the most accurate
        available picture of reality), and no candidate is launched.
        The group goes dark from this manager's own automatic
        replacement machinery until an operator intervenes (a manual
        `systemctl restart isadoraair-encoders` remains the recovery
        path, per this project's own established precedent), rather
        than the alternative of possibly running two competing
        processes against the same device. Returns True once the old
        child is confirmed gone (or there was none to begin with) --
        only then is any state actually cleared, below."""
        proc = self._procs.get(input_device)
        if proc is not None:
            pid = getattr(proc, "pid", None)
            confirmed_gone = False
            try:
                proc.terminate()
                proc.wait(timeout=5)
                confirmed_gone = True
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                    confirmed_gone = True
                except subprocess.TimeoutExpired:
                    confirmed_gone = False
                except (ProcessLookupError, OSError):
                    confirmed_gone = True
            except (ProcessLookupError, OSError):
                confirmed_gone = True

            if not confirmed_gone and pid is not None:
                # Last-resort liveness check, independent of proc.wait()
                # -- os.kill(pid, 0) raises ProcessLookupError if the
                # pid is genuinely gone even if something else made
                # wait() itself unreliable.
                try:
                    os.kill(pid, 0)
                    confirmed_gone = False
                except ProcessLookupError:
                    confirmed_gone = True
                except OSError:
                    confirmed_gone = False

            if not confirmed_gone:
                self._log(input_device, f"Could not confirm prior Liquidsoap process (pid={pid}) had stopped -- refusing to launch a replacement.", force=True)
                emit_event(
                    category="encoder", level="critical",
                    title=f"Encoder group '{input_device}' could not confirm prior process stopped",
                    detail={"input_device": input_device, "pid": pid, "reason": reason},
                    dedupe_key=f"encoder|intentional-stop-unconfirmed|{input_device}",
                )
                return False

        self._procs.pop(input_device, None)
        script_path = self._scripts.pop(input_device, None)
        self._unlink_script_best_effort(input_device, script_path)
        self._stabilized.pop(input_device, None)
        self._current.pop(input_device, None)
        self._running_fingerprint.pop(input_device, None)
        self._running_encoders.pop(input_device, None)
        self._clear_qualification_tracking(input_device)
        # Defense in depth: an intentional stop tears down whatever
        # candidate/rollback was in flight (e.g. this group is being
        # removed mid-probation) without going through that lifecycle's
        # own promote/rollback-succeeded/rollback-failed conclusion --
        # drop the flag here too so it can never survive to misattribute
        # a later, unrelated ordinary Phase 2 launch as reconciliation-
        # originated.
        self._reconcile_origin.pop(input_device, None)
        self._mark_audio_state_dead(input_device)
        self._write_group_state(input_device)
        self._log(input_device, f"Intentionally stopped ({reason}).", force=True)
        emit_event(
            category="encoder", level="info",
            title=f"Encoder group '{input_device}' intentionally stopped",
            detail={"input_device": input_device, "reason": reason},
            dedupe_key=f"encoder|intentional-stop|{input_device}",
        )
        return True

    def _remove_group_intentionally(self, input_device):
        """Phase 3C "removed": no enabled Encoder rows remain for a
        currently-known group. Stops its live child if it has one
        (via _stop_group_intentionally -- same non-crash bookkeeping),
        then discards EVERY piece of this group's retry/qualification/
        rejection bookkeeping so a later re-enable starts completely
        fresh rather than resuming mid-escalation from a sequence
        that's since become meaningless. Its persistent LKG is
        deliberately left on disk (no safety reason to delete it; a
        re-enable of the identical configuration gets its fast path
        back immediately). Also used by _retry_group's own "no enabled
        encoders left" branch (replacing what used to be separate,
        near-identical inline cleanup there) -- one implementation of
        "forget about this group" instead of two.

        Post-commit review-fix: _stop_group_intentionally's own safety
        contract is "if the old child's death can't be confirmed,
        change NOTHING -- leave reality exactly as it was" (see its own
        docstring). This function used to ignore that return value
        entirely and unconditionally forget the group's state and
        announce it removed regardless -- exactly the "preserve
        reality" contract it's supposed to honor, silently violated by
        its own caller. Now aborts before touching ANY bookkeeping if
        the stop wasn't confirmed: no retry/rejection/fingerprint
        state cleared, no "removed" event, no group-state deletion --
        the group stays fully intact and eligible to have removal
        retried on a later reconciliation tick, never silently
        forgotten while a process might still be alive. Returns True
        on successful removal, False if aborted."""
        if input_device in self._procs:
            if not self._stop_group_intentionally(input_device, reason="no enabled encoders remain for this input device"):
                # Already logged/emitted critical inside
                # _stop_group_intentionally; record the removal-level
                # outcome too, but change nothing else -- this group
                # is NOT removed, its state is NOT forgotten.
                self._record_reconcile_outcome(input_device, "remove_failed", "could not confirm prior process had stopped")
                return False
        self._retry_index.pop(input_device, None)
        self._retry_at.pop(input_device, None)
        self._meta.pop(input_device, None)
        self._launch_kind.pop(input_device, None)
        self._critical_stopped.pop(input_device, None)
        self._rejected_fingerprints.pop(input_device, None)
        self._candidate_fingerprint.pop(input_device, None)
        self._candidate_encoders.pop(input_device, None)
        self._running_fingerprint.pop(input_device, None)
        self._running_encoders.pop(input_device, None)
        self._accepted_fingerprint.pop(input_device, None)
        self._desired_fingerprint.pop(input_device, None)
        self._reconcile_origin.pop(input_device, None)
        self._clear_qualification_tracking(input_device)
        self._log(input_device, "No enabled encoders left for this device, not restarting.", force=True)
        emit_event(
            category="encoder", level="info",
            title=f"Encoder group '{input_device}' removed",
            detail={"input_device": input_device},
            dedupe_key=f"encoder|group-removed|{input_device}",
        )
        try:
            _group_state_path(input_device).unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def _reconcile_changed_group(self, input_device, desired_encoders, desired_fp):
        """Phase 3D-G: replace an ALREADY-RUNNING group's child with a
        newly-desired configuration, without ever double-launching
        against the same input_device. Every static check (rejected-
        fingerprint short-circuit, cross-group destination collision,
        validation, preflight) runs BEFORE the current healthy child
        is touched -- only once every one of them passes is the old
        child intentionally stopped and the EXACT preflighted
        candidate script launched (via _static_check_candidate +
        script_override, same discipline _launch_group's own candidate
        pipeline now follows), handing off to the unmodified Phase 2
        qualification/promotion/rollback machinery from there."""
        rejected = self._rejected_fingerprints.get(input_device, set())
        if desired_fp in rejected:
            # Known-bad -- do nothing. Unlike _launch_group's bootstrap
            # case, there is no "must launch SOMETHING" pressure here:
            # the group is already running its current accepted/LKG
            # configuration, which simply stays as-is.
            return

        if self._cross_group_collision_blocked(input_device, desired_encoders, desired_fp):
            return  # re-evaluated fresh next tick -- not sticky (see _cross_group_collision_blocked)

        ok, candidate_script, failure_kind, reason, detail = self._static_check_candidate(input_device, desired_encoders)
        if not ok:
            self._reject_prelaunch_candidate(input_device, desired_encoders, desired_fp, failure_kind, reason, detail=detail)
            self._record_reconcile_outcome(input_device, f"static_{failure_kind}_rejected", reason)
            return

        self._record_reconcile_outcome(input_device, "replacement_started")
        if not self._stop_group_intentionally(input_device, reason=f"replacing with newly-preflighted configuration (fingerprint {desired_fp[:12]})"):
            # Phase 3F: could not confirm the old process is gone --
            # already logged/emitted critical inside
            # _stop_group_intentionally; refuse to launch a second
            # process against the same device.
            self._record_reconcile_outcome(input_device, "candidate_failed", "could not confirm prior process had stopped")
            return

        # From this point on, the old proven child is confirmed gone and
        # we are committed to a candidate/rollback lifecycle for this
        # input device -- mark it as reconciliation-originated so the
        # Phase 2 promotion/rollback completion paths below (shared
        # verbatim with ordinary cold-boot/retry) know this particular
        # in-flight attempt's eventual outcome belongs in
        # last_reconcile_result. Set here (not only on launch success)
        # so the immediate-Popen-failure-then-rollback branch right
        # below is covered too, not just the launch-succeeded tail.
        self._reconcile_origin[input_device] = True

        ok = self._start_group(input_device, desired_encoders, script_override=candidate_script)
        if not ok:
            # Phase 3G: a Popen() failure here happens AFTER the old
            # proven child is already gone -- this is NOT an ordinary
            # retry condition. Immediately fall back to Phase 2
            # rollback (exact reuse, same function _reject_live_
            # candidate already calls for a live qualification
            # failure).
            self._record_reconcile_outcome(input_device, "candidate_failed", "failed to launch replacement candidate")
            self._start_rollback(input_device, desired_encoders, "failed to launch replacement candidate immediately after intentional stop during reconciliation")
            return

        self._launch_kind[input_device] = "candidate"
        self._candidate_fingerprint[input_device] = desired_fp
        self._candidate_encoders[input_device] = desired_encoders
        self._qualify_expected[input_device] = desired_encoders
        self._running_fingerprint[input_device] = desired_fp
        self._start_qualification_clock(input_device)
        self._write_group_state(input_device)
        self._record_reconcile_outcome(input_device, "candidate_launched")
        self._log(input_device, f"Reconciliation: replacement candidate launched (fingerprint={desired_fp[:12]}), qualification in progress.", force=True)
        emit_event(
            category="encoder", level="info",
            title=f"Encoder group '{input_device}' replacement candidate launched",
            detail={
                "input_device": input_device, "fingerprint": desired_fp,
                "generation": self._current[input_device]["generation"],
                "encoder_names": [e.name for e in desired_encoders],
            },
            dedupe_key=f"encoder|reconcile-candidate-launched|{input_device}",
        )

    def _reconcile(self):
        """Phase 3P: the reconciliation scan itself -- cheap (one
        queryset, grouping, in-memory fingerprint comparisons), called
        every _check_health() tick (same 5s cadence as everything
        else). Qualification of anything it launches remains entirely
        tick-driven exactly as Phase 2 already does it (see
        _check_candidate_qualification, unmodified and unaware of how
        a candidate came to be launched) -- this method itself never
        blocks waiting for health.

        Has its own top-level exception containment (unlike the
        per-device actions below, which _check_health wraps via
        _guarded) because the desired-topology query/classification
        that precedes any single device's action isn't itself
        per-device -- a failure there (e.g. a DB connectivity blip)
        must not crash the supervisor any more than a per-device
        failure would."""
        try:
            self._reconcile_inner()
        except Exception as exc:
            print(f"  [reconcile] Unexpected error during reconciliation scan: {exc}")
            emit_event(
                category="encoder", level="critical",
                title="Encoder reconciliation scan failed unexpectedly",
                detail={"error": repr(exc)},
                dedupe_key="encoder|reconciliation-scan-error",
            )

    def _reconcile_inner(self):
        from . import lkg as lkg_module
        from .validation import normalized_destination_key

        close_old_connections()
        desired_groups = _group_by_input_device(Encoder.objects.filter(enabled=True))
        known_devices = (
            set(self._procs) | set(self._retry_at) | set(self._meta)
            | set(self._current) | set(self._launch_kind)
        )

        # Phase 3C "removed" -- instant and safe regardless of any
        # other group's in-flight transition; never resurrect these.
        for input_device in sorted(known_devices - set(desired_groups)):
            self._guarded(input_device, "reconciliation (removed group)", lambda d=input_device: self._remove_group_intentionally(d))

        known_devices = (
            set(self._procs) | set(self._retry_at) | set(self._meta)
            | set(self._current) | set(self._launch_kind)
        )

        # Phase 3K: at most one topology REPLACEMENT in flight at a
        # time -- derived for free from launch_kind rather than a
        # separate lock: a group currently "candidate" or "rollback"
        # IS a transition in progress, and _check_candidate_
        # qualification (unaffected by any of this) keeps driving it
        # to completion every tick regardless. While any group is
        # transitioning, no NEW replacement/bootstrap starts this
        # tick; ordinary health/crash-retry supervision of every other
        # group continues completely unaffected (it lives in
        # _check_health's other, unconditional loops).
        transitioning = {d for d, k in self._launch_kind.items() if k in ("candidate", "rollback")}

        # Phase 3 review-fix pass, Issue 2: an intrinsically ambiguous
        # DESIRED topology (two groups' current desired rows both
        # claiming the same normalized destination) must be caught
        # BEFORE either one is ever dispatched -- otherwise
        # reconciliation order (the priority sort below) would let
        # whichever one launches first silently "win," with the other
        # then blocked only by the SEPARATE running-state collision
        # check on a later tick. Computed purely from `desired_groups`
        # (no running/live state involved at all), which is exactly
        # what makes it structurally incapable of misfiring on a
        # legitimate input-device move: a moved Encoder row can only
        # ever appear in ONE group's desired set at a time (its
        # input_device is a single value), so desired(old) and
        # desired(new) never actually overlap once the move has
        # committed to the DB -- only two genuinely independent rows
        # configured with the same destination produce a real conflict
        # here.
        desired_conflicts = self._desired_vs_desired_conflicts(desired_groups)
        for input_device, other_devices in sorted(desired_conflicts.items()):
            self._guarded(
                input_device, "reconciliation (desired collision)",
                lambda d=input_device, others=other_devices: self._report_desired_conflict(d, others),
            )

        candidates = []
        for input_device, encoders in desired_groups.items():
            desired_fp = lkg_module.compute_fingerprint(input_device, encoders)
            self._desired_fingerprint[input_device] = desired_fp
            if input_device in desired_conflicts:
                # Ambiguous desired topology -- neither side may be
                # dispatched until the operator resolves it (Issue 2).
                # Unrelated groups (not part of any conflict) are
                # completely unaffected and remain eligible below.
                continue
            if input_device not in known_devices:
                candidates.append(("new", input_device, encoders, desired_fp))
                continue
            if input_device in self._retry_at:
                # Already on the ordinary retry/backoff schedule --
                # that path's own _launch_group call already re-reads
                # DB state fresh each attempt, so it naturally picks up
                # the latest desired configuration on its own timer.
                # No separate reconciliation action needed (and
                # starting one here would just fight the backoff).
                continue
            running_fp = self._running_fingerprint.get(input_device)
            if running_fp is not None and desired_fp == running_fp:
                # Phase 3 review-fix pass, Issue 1: self-heal a stale/
                # incomplete _running_encoders cache (e.g. left over
                # from a rollback whose LKG metadata predates protocol/
                # mount -- see lkg.destinations_from_lkg_meta) the
                # instant desired is PROVEN to match what's running
                # again. desired_fp == running_fp is itself the proof:
                # identical fingerprints mean `encoders` (the DB rows
                # just fetched fresh, above) describe the exact same
                # runtime-affecting configuration already running --
                # never a rejected/differing one, which would produce a
                # different fingerprint by construction. Cheap (an
                # in-memory list swap, the DB read already happened for
                # this tick) and closes the legacy-metadata cross-group-
                # collision blind spot without ever touching the LKG
                # file or trusting DB values for anything but this
                # exact proven-matching case -- still Phase 3C's
                # "otherwise do absolutely nothing": no launch, no
                # generation change, no state churn beyond this cache.
                cached = self._running_encoders.get(input_device)
                if cached and not all(getattr(e, "protocol", None) is not None for e in cached):
                    self._running_encoders[input_device] = encoders
                continue  # Phase 3C "unchanged" -- do absolutely nothing
            candidates.append(("changed", input_device, encoders, desired_fp))

        if not candidates or transitioning:
            return

        def priority(item):
            _kind, input_device, encoders, _fp = item
            desired_keys = {k for k in (normalized_destination_key(e) for e in encoders) if k is not None}
            running_keys = {
                k for k in (normalized_destination_key(e) for e in self._running_encoders.get(input_device, []))
                if k is not None
            }
            return (len(desired_keys - running_keys), input_device)

        candidates.sort(key=priority)

        for kind, input_device, encoders, desired_fp in candidates:
            if self._cross_group_collision_blocked(input_device, encoders, desired_fp):
                continue  # try the next candidate this tick; this one waits for the colliding group to relinquish first
            if kind == "new":
                def start_new(d=input_device, e=encoders):
                    if not self._launch_group(d, e):
                        self._schedule_retry(d)
                self._guarded(input_device, "reconciliation (new group)", start_new)
            else:
                self._guarded(input_device, "reconciliation (changed group)", lambda d=input_device, e=encoders, fp=desired_fp: self._reconcile_changed_group(d, e, fp))
            break  # Phase 3K: only one transition starts per tick

    def _guarded(self, input_device, context, fn):
        """Defense-in-depth safety net (Phase 2 review-fix pass 2,
        Issue 3): runs `fn()`, catching and logging/reporting ANY
        exception rather than letting it propagate out of
        _check_health(). Every specific, expected failure mode this
        pass identified already has its own precise, contained handling
        deeper in the call stack (see _launch_group, _start_group,
        _promote_candidate, _start_rollback, _on_qualification_failed);
        this is the outer backstop for whatever wasn't anticipated --
        _check_health's own main loop has NOTHING above it catching
        exceptions (EncoderManager.start()'s `while self.running: ...`
        has no try/except, and neither does run_encoders.py's own
        handle()), so an escape here would take down the entire
        supervisor and everything it's currently running. One group's
        unexpected failure must never prevent this same tick from
        still checking every OTHER group."""
        try:
            fn()
        except Exception as exc:
            self._log(input_device, f"Unexpected error during {context}: {exc}", force=True)
            emit_event(
                category="encoder", level="critical",
                title=f"Encoder group '{input_device}' unexpected error during {context}",
                detail={"input_device": input_device, "context": context, "error": repr(exc)},
                dedupe_key=f"encoder|unexpected-error|{input_device}|{context}",
            )

    def _check_health(self):
        now_mono = time.monotonic()

        # Phase 3: reconciliation scan first -- topology decisions
        # (removed/new/changed groups) before this tick's per-process
        # health details, so e.g. a just-removed group's proc isn't
        # also processed by the stabilization/qualification loops
        # below in the same tick. Has its own internal exception
        # containment (see _reconcile's own docstring).
        self._reconcile()

        # Dead processes -- handle each independently; with multiple
        # device groups, blocking here on one would delay noticing the
        # others (Phase 7 req #4/#12).
        for input_device, proc in list(self._procs.items()):
            if proc.poll() is not None:
                self._guarded(input_device, "exit handling", lambda d=input_device, rc=proc.returncode: self._handle_exit(d, rc))

        # Retries whose backoff has elapsed -- re-read the current DB
        # state rather than reusing the encoder list from whenever this
        # group last started, in case rows changed since.
        for input_device, at in list(self._retry_at.items()):
            if now_mono < at:
                continue
            del self._retry_at[input_device]
            self._guarded(input_device, "retry", lambda d=input_device: self._retry_group(d))

        # Stabilization check for every currently-running child (Phase 1
        # backoff-reset -- unchanged, audio-only, applies regardless of
        # launch_kind).
        for input_device in list(self._procs.keys()):
            self._guarded(input_device, "stabilization check", lambda d=input_device: self._check_stabilization(d))

        # Candidate qualification check (Phase 2H/2I) -- only meaningful
        # for a group currently on probation ("candidate" or "rollback");
        # a no-op read for an "accepted" group.
        for input_device in list(self._procs.keys()):
            self._guarded(input_device, "candidate qualification check", lambda d=input_device: self._check_candidate_qualification(d))

        # Heartbeat: refresh every currently-tracked group's state file
        # on every tick, not just when something changed -- this file's
        # own `timestamp` is the tight-staleness "is the manager loop
        # itself alive and still supervising this group" signal
        # monitoring relies on (Phase 8 req #4). A long, uneventful
        # healthy run must not let it go stale just because nothing
        # happened. (_write_group_state already contains its own
        # exceptions internally; _guarded here is pure defense in depth.)
        for input_device in list(self._procs.keys()):
            self._guarded(input_device, "state heartbeat", lambda d=input_device: self._write_group_state(d))

    def _retry_group(self, input_device):
        """One retry-loop iteration's worth of work, extracted from
        _check_health so _guarded (above) can wrap it as a single unit
        -- re-reads current DB state fresh (in case rows changed since
        this group last started) rather than reusing whatever encoder
        list was true at an earlier launch."""
        close_old_connections()
        groups = _group_by_input_device(Encoder.objects.filter(enabled=True))
        encoders = groups.get(input_device)
        if not encoders:
            # Phase 7 req #10/#11 / Phase 3C "removed": a group with no
            # enabled encoders left must not be resurrected -- and its
            # retry/backoff bookkeeping is dropped entirely, not just
            # skipped this once, so a LATER re-enable starts the
            # backoff schedule fresh rather than resuming mid-
            # escalation from a retry sequence that's since become
            # meaningless. Shared with _reconcile_inner's own
            # "removed" classification -- one implementation.
            self._remove_group_intentionally(input_device)
            return
        self._log(input_device, "Retrying...", force=True)
        if not self._launch_group(input_device, encoders):
            self._schedule_retry(input_device)
