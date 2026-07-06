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
# independent Loopback card added alongside the existing Studio Monitor
# <-> StereoTool one — see PROJECT_NOTES.md for the exact card layout).
DEFAULT_INPUT_DEVICE = "plughw:3,1"

HEALTH_CHECK_SECONDS = 5
RESTART_DELAY_SECONDS = 10
SCRIPT_DIR = Path("/run/isadoraair/liquidsoap")

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
        return f"%mp3(bitrate={encoder.bitrate_kbps})"
    if encoder.format == "aac":
        return f'%ffmpeg(format="adts", %audio(codec="aac", b="{encoder.bitrate_kbps}k"))'
    return f"%vorbis(bitrate={encoder.bitrate_kbps})"  # vorbis


def _output_block(encoder, source_var):
    fmt = _format_block(encoder)
    common = (
        f"host={_liq_string(encoder.host)}, port={encoder.port}, "
        f"password={_liq_string(encoder.password)}, "
        f"name={_liq_string(encoder.station_name)}, "
        f"genre={_liq_string(encoder.genre)}, "
        f"url={_liq_string(encoder.url)}, "
        f"public={'true' if encoder.public else 'false'}"
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


def build_liquidsoap_script(input_device, encoders):
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
    lines = [
        f'source = input.alsa(device={_liq_string(input_device)})',
        'source = blank.detect(threshold=-40.0, max_blank=20.0, min_noise=0.5, source)',
        '',
        'def write_silence_state(is_blank) =',
        f'  state = json.stringify(compact=true, {{is_blank = is_blank, timestamp = time()}})',
        f'  file.write(data=state, atomic=true, {_liq_string(state_path)})',
        'end',
        '',
        'source.on_blank(synchronous=false, {write_silence_state(true)})',
        'source.on_noise(synchronous=false, {write_silence_state(false)})',
        # on_blank/on_noise only fire on a transition -- without this,
        # a feed that's been continuously fine since startup would never
        # write a state file at all, and the dashboard would show
        # "unknown" forever instead of "ok". start_blank defaults to
        # false, so the matching initial assumption here is "not blank".
        'write_silence_state(false)',
        '',
    ]
    lines += [_output_block(encoder, "source") for encoder in encoders]
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
        script = build_liquidsoap_script(input_device, encoders)
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
