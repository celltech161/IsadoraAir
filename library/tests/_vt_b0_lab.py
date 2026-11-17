"""Roadmap 2.25 Phase B0 -- isolated real-GStreamer timing lab.

Test support only (no ``test`` prefix). Nothing here is imported by
production code. It reuses the 2.23 transport harness (real engine object,
real ``_create_deck`` deck bins with the 0.3 s silence prime, real
``audiomixer`` program topology with live silence beds and a clock-synced
appsink) and adds:

* sample-accurate rendered-audio measurement: a recursive sliding DFT over
  the recorded program PCM, 10 ms window (``rate // 100`` samples), so every
  tone at a multiple of 100 Hz is exactly orthogonal to the others and an
  abrupt onset or end is located at the 50 % amplitude crossing (sub-ms);
* ``ScheduledBranch``: a prototype VT branch admitted to ``fx_submix`` at a
  chosen future running time by a pad offset, with readiness (first buffer)
  evidence, an owner token, bounded deadlines, error routing, cancellation
  with an optional fade, and detach-first retirement;
* deck creation at a chosen running-time offset through the REAL
  ``_create_deck`` (only ``_apply_pad_offset`` is replaced for that call);
* a live "microphone" stand-in on ``master_mixer`` and a speech-owned duck
  on ``vt_duck_gain`` driven by a GstController curve in running time.

Every timing claim the B0 tests make is measured in the rendered PCM at
the appsink, against the planned running time -- never from a timer firing
or a state change.
"""

from __future__ import annotations

import bisect
import cmath
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import wave
from types import SimpleNamespace
from unittest.mock import patch

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstController", "1.0")
from gi.repository import GLib, Gst, GstController

import library.services.engine as eng_module
from library.tests import _transport_harness as th
from library.tests._transport_harness import TransportHarness, make_item, make_track, pump, wait_until

P = eng_module.SILENCE_PRIME_SECONDS
NS = Gst.SECOND


def report(label, **values):
    rendered = " ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in values.items())
    print(f"\nB0-METRIC {label} {rendered}", flush=True)


def stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    mean = sum(values) / len(values)
    return {"n": len(values), "mean": mean, "min": min(values), "max": max(values),
            "max_abs": max(abs(v) for v in values)}


# ---------------------------------------------------------------------------
# Media.
# ---------------------------------------------------------------------------
def write_segments(path, segments, *, rate, amplitude=0.4):
    """Stereo 16-bit WAV of consecutive (seconds, freq) segments, freq 0 =
    silence. Each tone segment starts at phase 0 (abrupt, measurable onset)."""
    peak = int(32767 * amplitude)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(rate)
        for seconds, freq in segments:
            frames = int(round(rate * seconds))
            chunk = bytearray()
            for index in range(frames):
                value = 0 if freq <= 0 else int(peak * math.sin(2 * math.pi * freq * index / rate))
                chunk += struct.pack("<hh", value, value)
            output.writeframes(bytes(chunk))
    return path


def to_flac(wav_path):
    flac = shutil.which("flac")
    if flac is None:
        raise RuntimeError("flac CLI not installed")
    out = wav_path.with_suffix(".flac")
    subprocess.run([flac, "-s", "-f", "-o", str(out), str(wav_path)], check=True,
                   stdin=subprocess.DEVNULL)
    return out


# ---------------------------------------------------------------------------
# Rendered-audio analysis.
# ---------------------------------------------------------------------------
class Trace:
    """The recorded program PCM (left channel) with running-time mapping."""

    def __init__(self, buffers, rate):
        self.rate = rate
        self.window = rate // 100
        self.starts = []        # sample index of each buffer
        self.times = []         # running time of each buffer start
        samples = []
        for running, values in buffers:
            self.starts.append(len(samples))
            self.times.append(running)
            samples.extend(values)
        self.x = samples

    def time_of(self, index):
        pos = bisect.bisect_right(self.starts, index) - 1
        pos = max(pos, 0)
        return self.times[pos] + (index - self.starts[pos]) / self.rate

    def index_of(self, running):
        pos = bisect.bisect_right(self.times, running) - 1
        pos = max(pos, 0)
        return self.starts[pos] + int(round((running - self.times[pos]) * self.rate))

    def amps(self, freq, i0, i1):
        """Amplitude of ``freq`` in the window starting at each index in
        [i0, i1) (recursive sliding DFT, re-seeded every 4096 steps)."""
        n = self.window
        x = self.x
        i0 = max(0, i0)
        i1 = min(i1, len(x) - n - 1)
        if i1 <= i0:
            return []
        k = freq * n / self.rate
        rot = cmath.exp(2j * math.pi * k / n)
        basis = [cmath.exp(-2j * math.pi * k * m / n) for m in range(n)]
        out = []
        acc = 0j
        for i in range(i0, i1):
            if (i - i0) % 4096 == 0:
                acc = sum(x[i + m] * basis[m] for m in range(n))
            out.append(2.0 * abs(acc) / n)
            acc = (acc - x[i] + x[i + n]) * rot
        return out

    def coarse(self, freq):
        """Per-buffer (10 ms) amplitude series: [(running, amp)] (cached)."""
        cache = self.__dict__.setdefault("_coarse_cache", {})
        if freq not in cache:
            cache[freq] = self._coarse(freq)
        return cache[freq]

    def _coarse(self, freq):
        result = []
        for pos, start in enumerate(self.starts):
            end = self.starts[pos + 1] if pos + 1 < len(self.starts) else len(self.x)
            seg = self.x[start:end]
            if not seg:
                continue
            result.append((self.times[pos], _goertzel(seg, freq, self.rate)))
        return result

    def onset(self, freq, after=0.0, before=1e9, threshold=0.03, sustain=3):
        """Running time at which ``freq`` becomes present (50 % crossing of
        its steady amplitude, window-centre corrected). None if absent."""
        series = [(t, a) for t, a in self.coarse(freq) if after <= t < before]
        hit = None
        for idx in range(len(series) - sustain + 1):
            if all(a >= threshold for _t, a in series[idx:idx + sustain]):
                hit = series[idx][0]
                break
        if hit is None:
            return None
        n = self.window
        centre = self.index_of(hit)
        i0 = max(self.index_of(after), centre - 3 * n)
        amps = self.amps(freq, i0, centre + 5 * n)
        if not amps:
            return hit
        steady = max(amps[-2 * n:]) if len(amps) > 2 * n else max(amps)
        for offset, amp in enumerate(amps):
            if amp >= 0.5 * steady:
                return self.time_of(i0 + offset + n // 2)
        return hit

    def end(self, freq, after, threshold=0.03, sustain=3):
        """Running time at which ``freq`` (present at ``after``) disappears."""
        series = [(t, a) for t, a in self.coarse(freq) if t >= after]
        hit = None
        for idx in range(len(series) - sustain + 1):
            if all(a < threshold for _t, a in series[idx:idx + sustain]):
                hit = series[idx][0]
                break
        if hit is None:
            return None
        n = self.window
        centre = self.index_of(hit)
        i0 = centre - 5 * n
        amps = self.amps(freq, i0, centre + 3 * n)
        if not amps:
            return hit
        steady = max(amps[:2 * n])
        for offset, amp in enumerate(amps):
            if offset > n and amp < 0.5 * steady:
                return self.time_of(i0 + offset + n // 2)
        return hit

    def level(self, freq, start, end):
        values = [a for t, a in self.coarse(freq) if start <= t < end]
        return sum(values) / len(values) if values else None

    def present(self, freq, start, end, threshold=0.03):
        return sum(1 for t, a in self.coarse(freq) if start <= t < end and a >= threshold) * 0.01


def _goertzel(samples, freq, rate):
    count = len(samples)
    k = int(0.5 + count * freq / rate)
    omega = 2.0 * math.pi * k / count
    coeff = 2.0 * math.cos(omega)
    s_prev = s_prev2 = 0.0
    for sample in samples:
        s = sample + coeff * s_prev - s_prev2
        s_prev2, s_prev = s_prev, s
    power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    return 2.0 * math.sqrt(max(power, 0.0)) / count


# ---------------------------------------------------------------------------
# The lab.
# ---------------------------------------------------------------------------
class Lab:
    def __init__(self, testcase, *, rate=44100, mic_freq=None):
        self.testcase = testcase
        self.rate = rate
        rate_patch = patch.object(th, "RATE", rate)
        rate_patch.start()
        testcase.addCleanup(rate_patch.stop)
        self.h = TransportHarness(testcase, appsink=True)
        self.engine = self.h.engine
        self.root = self.h.root
        self.branches = []
        self.owner = {}          # side -> token currently allowed to act
        self.events = []
        self.mic_gate = None
        if mic_freq:
            self._add_mic(mic_freq)
        bus = self.engine.main_pipeline.get_bus()
        bus.connect("message::error", self._route_branch_error)
        self._media_cache = {}

    # -- topology additions ---------------------------------------------------
    def _add_mic(self, freq):
        pipe = self.engine.main_pipeline
        src = Gst.ElementFactory.make("audiotestsrc", "lab_mic")
        src.set_property("is-live", True)
        src.set_property("freq", float(freq))
        src.set_property("volume", 0.4)
        caps = Gst.ElementFactory.make("capsfilter", None)
        caps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,rate={self.rate},channels=2"))
        gate = Gst.ElementFactory.make("volume", "lab_mic_gate")
        gate.set_property("volume", 0.0)
        for el in (src, caps, gate):
            pipe.add(el)
        src.link(caps)
        caps.link(gate)
        gate.get_static_pad("src").link(self.engine.master_mixer.request_pad_simple("sink_%u"))
        self.mic_gate = gate

    def stop(self):
        """End this lab's live pipeline early (the harness cleanup repeats it)."""
        for branch in self.branches:
            thread = getattr(branch, "null_thread", None)
            if thread is not None:
                thread.join(2.0)
        self.engine.main_pipeline.set_state(Gst.State.NULL)

    def play(self):
        self.h.play()
        assert wait_until(lambda: len(self.h.program_buffers) > 20, 5.0)

    # -- clock ----------------------------------------------------------------
    def now_ns(self):
        pipe = self.engine.main_pipeline
        clock = pipe.get_clock()
        if clock is None:            # stopped lab: late callbacks see the last time
            return getattr(self, "_last_now_ns", 0)
        self._last_now_ns = clock.get_time() - pipe.get_base_time()
        return self._last_now_ns

    def now(self):
        return self.now_ns() / NS

    def at(self, running_s, fn, *args):
        """GLib timer for running time ``running_s`` (main loop; jittery)."""
        delay = max(0, int(round((running_s - self.now()) * 1000)))
        GLib.timeout_add(delay, lambda: (fn(*args), False)[1])

    def trace(self):
        return Trace(list(self.h.program_buffers), self.rate)

    # -- media ------------------------------------------------------------------
    def media(self, name, segments, *, fmt="wav", media_rate=None, amplitude=0.4):
        key = (name, tuple(segments), fmt, media_rate, amplitude)
        if key in self._media_cache:
            return self._media_cache[key]
        path = self.root / f"{name}.wav"
        write_segments(path, segments, rate=media_rate or 44100, amplitude=amplitude)
        if fmt == "flac":
            path = to_flac(path)
        self._media_cache[key] = path
        return path

    def item(self, item_id, path, *, duration, cue_in=0.0, next_start=None, intro_until=0.0):
        track = make_track(path, 5000 + item_id, duration=duration, cue_in=cue_in, next_start=next_start)
        track.intro_until_seconds = intro_until
        return make_item(track, item_id)

    # -- decks ------------------------------------------------------------------
    def create_deck(self, slot, item, offset_ns=None, readiness=None, stall_s=0.0):
        """The REAL _create_deck. ``offset_ns`` (running time of the deck's
        first output sample = start of its silence prime) replaces only
        _apply_pad_offset's "now". ``readiness`` (dict) receives the running
        time at which the deck's first decoded (post-prime) buffer reached
        the real stage -- the evidence that it is prepared."""
        engine = self.engine
        original = engine._apply_pad_offset
        record = {"created_ns": self.now_ns()}

        def apply(deck_bin, internal_position_ns=0):
            if offset_ns is None:
                original(deck_bin, internal_position_ns=internal_position_ns)
                record["offset_ns"] = deck_bin.get_static_pad("src").get_offset()
            else:
                deck_bin.get_static_pad("src").set_offset(offset_ns)
                record["offset_ns"] = offset_ns

        def before_sync(deck):
            if stall_s:
                # Delayed preparation stand-in: decoded audio is held at the
                # deck's real stage for ``stall_s`` (removable blocking probe).
                stall_pad = deck.seek_gate_pad
                stall_id = stall_pad.add_probe(
                    Gst.PadProbeType.BLOCK_DOWNSTREAM | Gst.PadProbeType.BUFFER,
                    lambda *_a: Gst.PadProbeReturn.OK)
                GLib.timeout_add(int(stall_s * 1000), lambda: (stall_pad.remove_probe(stall_id), False)[1])
            if readiness is None:
                return

            def probe(_pad, info):
                if "ready_ns" not in readiness:
                    readiness["ready_ns"] = self.now_ns()
                return Gst.PadProbeReturn.REMOVE
            deck.seek_gate_pad.add_probe(Gst.PadProbeType.BUFFER, probe)

        engine._apply_pad_offset = apply
        try:
            deck = engine._create_deck(slot, item, before_sync=before_sync)
        finally:
            engine._apply_pad_offset = original
        if deck is not None:
            deck.lab = record
        return deck

    def resources(self):
        def count(path):
            try:
                return len(os.listdir(path))
            except OSError:
                return -1
        return {"deck_pads": len(self.engine.mixer.sinkpads),
                "fx_pads": len(self.engine.fx_submix.sinkpads),
                "children": len(self.engine.main_pipeline.children),
                "threads": count("/proc/self/task"), "fds": count("/proc/self/fd")}

    def mixer_pad_count(self):
        return len(self.engine.mixer.sinkpads), len(self.engine.fx_submix.sinkpads)

    def child_count(self):
        return len(self.engine.main_pipeline.children)

    # -- branches ---------------------------------------------------------------
    def branch(self, path, *, side, target_ns, duration, gain=1.0, ready_margin_ms=20,
               completion_grace_s=2.0, first_audio_s=1.0, mode="scheduled", mixer=None,
               stall_s=0.0):
        branch = ScheduledBranch(self, path, side=side, target_ns=target_ns, duration=duration,
                                 gain=gain, ready_margin_ms=ready_margin_ms,
                                 completion_grace_s=completion_grace_s, first_audio_s=first_audio_s,
                                 mode=mode, mixer=mixer or self.engine.fx_submix, stall_s=stall_s)
        self.branches.append(branch)
        return branch

    def _route_branch_error(self, _bus, message):
        for branch in self.branches:
            if branch.owns(message.src):
                GLib.idle_add(branch._failed, branch.token, f"bus_error:{message.parse_error()[0].message}")
                return

    # -- speech-owned duck ---------------------------------------------------
    def attach_duck_curve(self):
        """One InterpolationControlSource on vt_duck_gain.volume, attached
        once and never removed (points are replaced). Times are in the deck
        mixer's output time, which equals pipeline running time here."""
        source = GstController.InterpolationControlSource()
        source.set_property("mode", GstController.InterpolationMode.LINEAR)
        binding = GstController.DirectControlBinding.new_absolute(self.engine.vt_duck_gain, "volume", source)
        self.engine.vt_duck_gain.add_control_binding(binding)
        source.set(0, 1.0)
        self.duck_source = source
        return source

    def duck_plan(self, points):
        """Replace the duck curve with [(running_s, gain)] points."""
        source = self.duck_source
        source.unset_all()
        for running_s, gain in points:
            source.set(int(running_s * NS), gain)


class ScheduledBranch:
    """Prototype VT branch: filesrc -> decodebin -> convert -> resample ->
    caps -> volume -> fx_submix, admitted at ``target_ns`` running time by a
    pad offset set BEFORE the branch starts. The first buffer arrives early
    and waits in the aggregator pad (readiness evidence); the aggregator
    renders it exactly when running time reaches the offset.

    Every callback carries the branch token; a token that is no longer the
    side's owner, or a state that has moved on, is ignored."""

    _serial = 0

    def __init__(self, lab, path, *, side, target_ns, duration, gain, ready_margin_ms,
                 completion_grace_s, first_audio_s, mode="scheduled", mixer=None, stall_s=0.0):
        ScheduledBranch._serial += 1
        self.lab = lab
        self.side = side
        self.token = (side, ScheduledBranch._serial)
        self.target_ns = target_ns
        self.duration = duration
        self.state = "planned"
        self.reason = None
        self.ready_ns = None
        self.first_pts = None
        self.eos_ns = None
        self.ended_ns = None
        self.ready_margin_ns = ready_margin_ms * Gst.MSECOND
        self.completion_grace_s = completion_grace_s
        self.first_audio_s = first_audio_s
        self.teardown_ms = None
        self.ignored = []
        self.mode = mode           # scheduled | link_now | gated | gated_scheduled
        self.block_id = None
        self.admitted_offset_ns = None
        engine = lab.engine
        rate = lab.rate
        name = f"b0_{self.token[1]}"
        self.filesrc = Gst.ElementFactory.make("filesrc", f"{name}_src")
        self.decodebin = Gst.ElementFactory.make("decodebin", f"{name}_dec")
        self.convert = Gst.ElementFactory.make("audioconvert", f"{name}_conv")
        self.resample = Gst.ElementFactory.make("audioresample", f"{name}_res")
        self.caps = Gst.ElementFactory.make("capsfilter", f"{name}_caps")
        self.caps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,rate={rate},channels=2"))
        self.gain = Gst.ElementFactory.make("volume", f"{name}_gain")
        self.gain.set_property("volume", gain)
        self.filesrc.set_property("location", str(path))
        self.mixer = mixer
        self.stall = None
        if stall_s:
            # Stalled preparation stand-in: the first decoded buffer is held
            # ``stall_s`` in a blocking probe (removable, so teardown never
            # waits on it).
            self.stall = stall_s
        self.elements = (self.filesrc, self.decodebin, self.convert, self.resample, self.caps, self.gain)
        self.null_ms = None
        lab.owner[side] = self.token

    def owns(self, obj):
        while obj is not None:
            if obj in self.elements:
                return True
            obj = obj.get_parent()
        return False

    def _current(self, token):
        return token == self.token and self.lab.owner.get(self.side) == token

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        """Prepare now; admission is at target_ns. Refuses a target that is
        not at least the ready margin in the future (head would be clipped)."""
        lab = self.lab
        engine = lab.engine
        if self.mode == "link_now":
            self.target_ns = lab.now_ns()
        elif self.target_ns - lab.now_ns() < self.ready_margin_ns:
            self.state = "failed"
            self.reason = "target_not_in_future"
            return False
        pipe = engine.main_pipeline
        for el in self.elements:
            pipe.add(el)
        self.filesrc.link(self.decodebin)
        self.convert.link(self.resample)
        self.resample.link(self.caps)
        self.caps.link(self.gain)
        self.fx_pad = self.mixer.request_pad_simple("sink_%u")
        src = self.gain.get_static_pad("src")
        src.link(self.fx_pad)
        src.set_offset(self.target_ns)
        if self.mode in ("gated", "gated_scheduled"):
            self.block_id = src.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, lambda *_a: Gst.PadProbeReturn.OK)
        if self.stall:
            stall_pad = self.caps.get_static_pad("src")
            self.stall_id = stall_pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM | Gst.PadProbeType.BUFFER,
                                                lambda *_a: Gst.PadProbeReturn.OK)

            def unstall():
                if getattr(self, "stall_id", None) is not None:
                    stall_pad.remove_probe(self.stall_id)
                    self.stall_id = None
                return False
            GLib.timeout_add(int(self.stall * 1000), unstall)
        token = self.token

        def on_pad_added(_el, pad):
            caps = pad.get_current_caps() or pad.query_caps(None)
            if caps and caps.get_size() and not caps.get_structure(0).get_name().startswith("audio"):
                return
            sink = self.convert.get_static_pad("sink")
            if not sink.is_linked():
                pad.link(sink)
        self.decodebin.connect("pad-added", on_pad_added)

        def ready_probe(_pad, info):
            # Upstream of any gate: the first decoded buffer exists.
            if self.ready_ns is None:
                self.ready_ns = lab.now_ns()
                self.first_pts = info.get_buffer().pts
                GLib.idle_add(self._ready, token)
            return Gst.PadProbeReturn.OK

        def buffer_probe(pad, info):
            buf = info.get_buffer()
            if buf.pts != Gst.CLOCK_TIME_NONE and buf.duration != Gst.CLOCK_TIME_NONE:
                # Running time at which this branch's last sample so far is rendered.
                self.last_end_ns = pad.get_offset() + buf.pts + buf.duration
            return Gst.PadProbeReturn.OK

        def eos_probe(_pad, info):
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.EOS:
                self.eos_ns = lab.now_ns()
                GLib.idle_add(self._eos, token)
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        self.caps.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, ready_probe)
        src.add_probe(Gst.PadProbeType.BUFFER, buffer_probe)
        src.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, eos_probe)
        self.state = "preparing"
        for el in self.elements:
            el.sync_state_with_parent()
        if self.mode == "gated_scheduled":
            # Held behind the gate (readiness provable, nothing anchored in
            # the mixer). Commit the planned offset COMMIT_LEAD before it.
            lab.at(self.target_ns / NS - self.COMMIT_LEAD, self._commit, token, self.target_ns)
            return True
        if self.mode != "scheduled":
            return True   # approach-comparison modes: no lifecycle deadlines
        # Deadlines, all in running time and owner-checked.
        ready_deadline = (self.target_ns - self.ready_margin_ns) / NS
        lab.at(ready_deadline, self._ready_deadline, token)
        lab.at(self.target_ns / NS + self.first_audio_s, self._first_audio_deadline, token)
        lab.at(self.target_ns / NS + self.duration + self.completion_grace_s,
               self._completion_deadline, token)
        return True

    COMMIT_LEAD = 0.100

    def _commit(self, token, offset_ns):
        if not self._current(token) or self.block_id is None:
            self.ignored.append(("commit", token))
            return
        self.commit(offset_ns)

    def commit(self, offset_ns):
        """Gated-scheduled: set the admission offset, then open the gate.
        The held buffer then waits in the mixer pad until ``offset_ns``."""
        src = self.gain.get_static_pad("src")
        self.committed_offset_ns = offset_ns
        src.set_offset(offset_ns)
        if self.block_id is not None:
            src.remove_probe(self.block_id)
            self.block_id = None

    def readmit(self, offset_ns):
        """Bring a held (not yet committed) branch forward. Refused once
        committed: a buffer queued in the mixer anchors the old time (B0 E09b)."""
        if self.block_id is None:
            return False
        self.target_ns = offset_ns
        self.commit(offset_ns)
        return True

    def admit(self):
        """Gated mode: release the held branch now (offset = current running
        time). Safe from any thread."""
        src = self.gain.get_static_pad("src")
        self.admitted_offset_ns = self.lab.now_ns()
        src.set_offset(self.admitted_offset_ns)
        if self.block_id is not None:
            src.remove_probe(self.block_id)
            self.block_id = None

    def _ready(self, token):
        if not self._current(token):
            self.ignored.append(("ready", token))
            return False
        if self.state == "preparing":
            self.state = "ready"
        return False

    def _ready_deadline(self, token):
        if not self._current(token):
            self.ignored.append(("ready_deadline", token))
            return
        if self.state == "preparing":
            # Not prepared before admission: retire without a clipped head.
            self._finish("failed", "not_ready_before_admission", fade_ms=0)
        elif self.state == "ready":
            self.state = "admitted"

    def _first_audio_deadline(self, token):
        if not self._current(token):
            self.ignored.append(("first_audio_deadline", token))
            return
        if self.state in ("preparing", "ready"):
            self._finish("failed", "no_first_audio", fade_ms=0)

    def _completion_deadline(self, token):
        if not self._current(token):
            self.ignored.append(("completion_deadline", token))
            return
        if self.state in ("preparing", "ready", "admitted", "draining"):
            self._finish("failed", "no_completion", fade_ms=0)

    def _eos(self, token):
        if not self._current(token):
            self.ignored.append(("eos", token))
            return False
        if self.state in ("ready", "admitted"):
            # EOS is seen when the last buffer is QUEUED in the aggregator
            # pad, not when it is rendered. Retire only after the last
            # sample has been rendered, or the tail is cut.
            last = getattr(self, "last_end_ns", None)
            self.state = "draining"
            if last is not None and last > self.lab.now_ns():
                self.lab.at(last / NS + 0.030, self._drained, token)
            else:
                self._finish("ended", "eos", fade_ms=0)
        return False

    def _drained(self, token):
        if not self._current(token):
            self.ignored.append(("drained", token))
            return
        if self.state == "draining":
            self._finish("ended", "eos", fade_ms=0)

    def _failed(self, token, reason):
        if not self._current(token):
            self.ignored.append(("error", token))
            return False
        if self.state not in ("ended", "failed", "cancelled"):
            self._finish("failed", reason, fade_ms=0)
        return False

    def cancel(self, reason="cancelled", fade_ms=50):
        if self.state in ("ended", "failed", "cancelled", "planned"):
            return
        audible = self.lab.now_ns() >= self.target_ns
        self._finish("cancelled", reason, fade_ms=fade_ms if audible else 0)

    def _finish(self, state, reason, fade_ms):
        self.state = state
        self.reason = reason
        self.ended_ns = self.lab.now_ns()
        self.lab.events.append((self.token, state, reason, self.ended_ns / NS))
        if fade_ms and getattr(self, "fx_pad", None) is not None:
            steps = max(1, fade_ms // 10)
            counter = [0]
            pad = self.fx_pad

            def step():
                counter[0] += 1
                pad.set_property("volume", max(0.0, 1.0 - counter[0] / steps))
                if counter[0] >= steps:
                    self._teardown()
                    return False
                return True
            pad.set_property("volume", 1.0 - 1.0 / steps)
            counter[0] = 1
            if steps == 1:
                self._teardown()
            else:
                GLib.timeout_add(10, step)
        else:
            self._teardown()
        if self.lab.owner.get(self.side) == self.token:
            self.lab.owner.pop(self.side, None)

    def _teardown(self):
        """Detach first: releasing the aggregator pad wakes a streaming
        thread that is waiting there with a held-back buffer, so the NULL
        transitions that follow cannot block on it."""
        if getattr(self, "fx_pad", None) is None:
            return
        began = time.monotonic()
        engine = self.lab.engine
        src = self.gain.get_static_pad("src")
        try:
            src.unlink(self.fx_pad)
        except Exception:
            pass
        self.mixer.release_request_pad(self.fx_pad)
        self.fx_pad = None
        if getattr(self, "stall_id", None) is not None:
            self.caps.get_static_pad("src").remove_probe(self.stall_id)
            self.stall_id = None
        if self.block_id is not None:
            # A gated branch still held by its blocking probe: let the held
            # buffer go to the (now unlinked) pad so the streaming thread
            # returns instead of waiting in the probe during NULL.
            src.remove_probe(self.block_id)
            self.block_id = None
        self.teardown_ms = (time.monotonic() - began) * 1000.0
        elements = self.elements
        pipeline = engine.main_pipeline

        def null_off_main_loop():
            # NULL transitions can wait on a streaming thread (a slow read,
            # a held buffer); they never run on the GLib main loop.
            started = time.monotonic()
            for el in elements:
                el.set_state(Gst.State.NULL)
            for el in elements:
                pipeline.remove(el)
            self.null_ms = (time.monotonic() - started) * 1000.0
        self.null_thread = threading.Thread(target=null_off_main_loop, daemon=True)
        self.null_thread.start()


__all__ = ["Lab", "ScheduledBranch", "Trace", "report", "stats", "P", "NS", "pump", "wait_until",
           "write_segments", "to_flac", "GLib", "Gst", "threading", "os", "SimpleNamespace"]
