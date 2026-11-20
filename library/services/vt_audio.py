"""Roadmap 2.25 B2 -- scheduled Voice Track audio building blocks.

Engine-agnostic GStreamer pieces, independently testable. Nothing here is
connected to live automatic transitions (that is B3/B4); the engine exposes
them through its ``_vt2_*`` methods.

* ``VtOwner`` -- the unambiguous identity of one prepared or playing VT.
* ``ScheduledVtFire`` -- a VT branch admitted to the FX submix at a planned
  running time by a pad offset set BEFORE the branch starts (B0-qualified),
  with readiness, first-audio and completion deadlines measured from the
  admission (a branch correctly waiting for a future admission is never
  "stalled"), drain-then-retire at the last rendered sample (F14),
  owner-checked callbacks, routed bus errors, cancellation with a fade, and
  detach-first retirement whose NULL transition runs off the GLib thread.
* ``VtDuckCurve`` -- the speech-owned duck: one control source bound once to
  ``vt_duck_gain.volume``; only its points change; one owner at a time.

Thread rules (engine invariants): every method here runs on the GLib main
thread except the pad-probe callbacks, which only record bounded state and
schedule GLib callbacks. Destructive operations never run on a streaming
thread; NULL transitions never run on the GLib thread.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstController", "1.0")
from gi.repository import GLib, Gst, GstController  # noqa: E402

from library.services import audio_recovery, vt_transition  # noqa: E402

NS = Gst.SECOND
MS = Gst.MSECOND

class FireState:
    PLANNED = "planned"        # constructed, nothing built
    PREPARING = "preparing"    # branch built and started, no decoded buffer yet
    READY = "ready"            # first buffer waiting in the mixer pad (scheduled)
    ADMITTED = "admitted"      # admission running time reached: audible
    DRAINING = "draining"      # EOS queued; waiting for the last sample to render
    ENDED = "ended"            # terminal: whole recording rendered
    CANCELLED = "cancelled"    # terminal
    FAILED = "failed"          # terminal

    TERMINAL = frozenset({ENDED, CANCELLED, FAILED})
    LIVE = frozenset({PREPARING, READY, ADMITTED, DRAINING})


@dataclass(frozen=True)
class VtOwner:
    """Who a VT belongs to. ``deck_slot``/``deck_generation`` identify the
    deck generation the speech is bound to (the outgoing deck for an outro,
    the claimed incoming deck for an intro)."""
    direction: str                 # vt_transition.OUTRO | vt_transition.INTRO
    log_item_id: int
    track_id: int
    deck_slot: str
    deck_generation: int
    transition_seq: int
    fire_id: int

    def __post_init__(self):
        if self.direction not in (vt_transition.OUTRO, vt_transition.INTRO):
            raise ValueError(f"unknown VT direction {self.direction!r}")


class ScheduledVtFire:
    """One scheduled VT branch. See the module docstring.

    ``now_ns()`` returns pipeline running time; ``timer_at(running_ns, fn,
    *args)`` schedules ``fn`` on the GLib thread at that running time;
    ``null_submit(fire, bin)`` hands the detached bin's NULL transition to a
    bounded off-thread worker AND its ownership: for every outcome it
    returns, the submit side owns the bin until NULL is confirmed or it is
    quarantined (engine: BoundedTeardownCoordinator ``retain=``);
    ``on_event(fire, kind)`` reports lifecycle changes to the owner layer."""

    DRAIN_GUARD_NS = 30 * MS
    FADE_STEP_MS = 10

    def __init__(self, *, owner: VtOwner, path, gain_db, admission_ns: int, validated_duration: float,
                 pipeline, mixer, rate: int, now_ns: Callable[[], int],
                 timer_at: Callable, null_submit: Callable, on_event: Optional[Callable] = None,
                 ready_lead_s: float = vt_transition.READY_LEAD_S,
                 first_audio_grace_s: float = vt_transition.FIRST_AUDIO_GRACE_S,
                 completion_grace_s: float = vt_transition.COMPLETION_GRACE_S,
                 before_start: Optional[Callable] = None):
        self.owner = owner
        self.path = str(path)
        self.gain = 10 ** ((gain_db or 0.0) / 20.0)
        self.admission_ns = int(admission_ns)
        self.duration = float(validated_duration)
        self._deadlines = vt_transition.fire_deadlines(self.admission_ns / NS, self.duration)
        self.ready_lead_ns = int(ready_lead_s * NS)
        self.first_audio_grace_ns = int(first_audio_grace_s * NS)
        self.completion_grace_ns = int(completion_grace_s * NS)
        self.pipeline = pipeline
        self.mixer = mixer
        self.rate = rate
        self._now_ns = now_ns
        self._timer_at = timer_at
        self._null_submit = null_submit
        self._on_event = on_event
        self.state = FireState.PLANNED
        self.reason = ""
        self.serial = 0                  # bumps on every terminal transition
        self.ready_ns = None
        self.last_end_ns = None
        self.eos_ns = None
        self.ended_ns = None
        self.retired = False
        self.detach_ms = None
        self.teardown_outcome = None
        self.teardown_errors = []
        self.ignored = []                # (callback, reason) for stale/late callbacks
        self.history = []                # (state, reason, running_ns)
        self.bin = None
        self.mixer_pad = None
        # Everything a built branch registers that references this fire (the
        # probe and signal closures capture it) or that keeps it alive (its
        # pending deadline timers); released at retirement so a retired fire
        # and its decoding chain can be collected.
        self._probe_handles = []          # [(pad, probe_id)]
        self._signal_handles = []         # [(element, handler_id)]
        self._timer_ids = []              # GLib source ids returned by timer_at
        self._fading = False
        self._before_start = before_start     # construction seam (tests): runs before the bin starts

    # -- identity -----------------------------------------------------------
    @property
    def fire_id(self):
        return self.owner.fire_id

    def owns(self, obj) -> bool:
        target = self.bin
        while obj is not None and target is not None:
            if obj == target:
                return True
            obj = obj.get_parent()
        return False

    def _mark(self, state, reason=""):
        self.state = state
        if reason:
            self.reason = reason
        self.history.append((state, reason, self._now_ns()))
        if self._on_event is not None:
            try:
                self._on_event(self, state)
            except Exception as exc:          # an owner bug must not break playout
                self.teardown_errors.append(f"on_event: {exc!r}")

    def _alive(self, token, name):
        """Owner check for every delayed callback: same fire, same epoch,
        not terminal. Anything else is a late/stale callback and is ignored."""
        if token != (self.fire_id, self.serial) or self.state in FireState.TERMINAL:
            self.ignored.append((name, self.state))
            return False
        return True

    # -- build / prepare -----------------------------------------------------
    def prepare(self) -> bool:
        """Build and start the branch now; admission is at ``admission_ns``.

        Refuses (FAILED, nothing built) when the admission is not at least
        the ready lead in the future: a late start would clip the head."""
        if self.state != FireState.PLANNED:
            return False
        if self.admission_ns - self._now_ns() < self.ready_lead_ns:
            self.serial += 1
            self._mark(FireState.FAILED, "admission_not_in_future")
            return False
        name = f"vt2_fire_{self.fire_id}"
        b = Gst.Bin.new(name)
        src = Gst.ElementFactory.make("filesrc", None)
        dec = Gst.ElementFactory.make("decodebin", None)
        conv = Gst.ElementFactory.make("audioconvert", None)
        res = Gst.ElementFactory.make("audioresample", None)
        caps = Gst.ElementFactory.make("capsfilter", None)
        vol = Gst.ElementFactory.make("volume", None)
        caps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,rate={self.rate},channels=2"))
        vol.set_property("volume", self.gain)
        src.set_property("location", self.path)
        for el in (src, dec, conv, res, caps, vol):
            b.add(el)
        src.link(dec)
        conv.link(res)
        res.link(caps)
        caps.link(vol)
        ghost = Gst.GhostPad.new("src", vol.get_static_pad("src"))
        b.add_pad(ghost)

        def on_pad_added(_el, pad):
            caps_now = pad.get_current_caps() or pad.query_caps(None)
            if caps_now and caps_now.get_size() and not caps_now.get_structure(0).get_name().startswith("audio"):
                return
            sink = conv.get_static_pad("sink")
            if not sink.is_linked():
                pad.link(sink)
        self._signal_handles.append((dec, dec.connect("pad-added", on_pad_added)))

        token = (self.fire_id, self.serial)
        vol_src = vol.get_static_pad("src")

        def buffer_probe(_pad, info):
            # Streaming thread: bounded state + one GLib handoff only.
            buf = info.get_buffer()
            if self.ready_ns is None:
                self.ready_ns = self._now_ns()
                GLib.idle_add(self._on_ready, token)
            if buf.pts != Gst.CLOCK_TIME_NONE and buf.duration != Gst.CLOCK_TIME_NONE:
                self.last_end_ns = self.admission_ns + buf.pts + buf.duration
            return Gst.PadProbeReturn.OK

        def eos_probe(_pad, info):
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.EOS:
                self.eos_ns = self._now_ns()
                GLib.idle_add(self._on_eos, token)
                return Gst.PadProbeReturn.DROP    # never reaches the live submix
            return Gst.PadProbeReturn.OK

        self._probe_handles.append((vol_src, vol_src.add_probe(Gst.PadProbeType.BUFFER, buffer_probe)))
        self._probe_handles.append((vol_src, vol_src.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, eos_probe)))

        self.bin = b
        self.pipeline.add(b)
        self.mixer_pad = self.mixer.request_pad_simple("sink_%u")
        ghost.link(self.mixer_pad)
        # The B0-qualified admission: the offset is the planned running time,
        # set before any buffer can flow.
        ghost.set_offset(self.admission_ns)
        self._mark(FireState.PREPARING)
        if self._before_start is not None:
            self._before_start(self, {"filesrc": src, "decodebin": dec, "caps": caps, "volume": vol})
        if not b.sync_state_with_parent():
            self._fail(token, "state_change_failure")
            return False
        d = self._deadlines
        self._schedule(int(d["ready_by"] * NS), self._ready_deadline, token)
        self._schedule(self.admission_ns, self._admission, token)
        self._schedule(int(d["first_audio_by"] * NS), self._first_audio_deadline, token)
        self._schedule(int(d["complete_by"] * NS), self._completion_deadline, token)
        return True

    def _schedule(self, running_ns, fn, token):
        """``timer_at`` plus bookkeeping: an id stays in ``_timer_ids`` only
        until its callback runs (GLib reuses source ids, so a dispatched id is
        never destroyed later), letting retirement cancel what is still
        pending."""
        seen = {"ran": False, "id": None}

        def run(tok):
            seen["ran"] = True
            if seen["id"] in self._timer_ids:
                self._timer_ids.remove(seen["id"])
            return fn(tok)
        source = self._timer_at(running_ns, run, token)
        if isinstance(source, int) and source > 0 and not seen["ran"]:
            seen["id"] = source
            self._timer_ids.append(source)

    def _cancel_timers(self):
        """Destroy this fire's still-pending deadline timers (every one is
        stale after a terminal transition; left alone they would keep the
        retired fire alive until their due time)."""
        context = GLib.MainContext.default()
        pending, self._timer_ids = self._timer_ids, []
        for source_id in pending:
            try:
                source = context.find_source_by_id(source_id)
                if source is not None and not source.is_destroyed():
                    source.destroy()
            except Exception as exc:
                self.teardown_errors.append(f"timer: {exc!r}")

    def _release_registrations(self):
        """Remove the pad probes and disconnect the decoder handler (both
        capture this fire). GLib thread, after the branch is unlinked: a probe
        callback already running on a streaming thread finishes normally and
        its GLib hand-off carries a stale token. Idempotent."""
        probes, self._probe_handles = self._probe_handles, []
        for pad, probe_id in probes:
            try:
                pad.remove_probe(probe_id)
            except Exception as exc:
                self.teardown_errors.append(f"remove_probe: {exc!r}")
        handlers, self._signal_handles = self._signal_handles, []
        for element, handler_id in handlers:
            try:
                if element.handler_is_connected(handler_id):
                    element.disconnect(handler_id)
            except Exception as exc:
                self.teardown_errors.append(f"disconnect: {exc!r}")

    # -- callbacks (GLib thread, owner-checked) ------------------------------
    def _on_ready(self, token):
        if self._alive(token, "ready") and self.state == FireState.PREPARING:
            self._mark(FireState.READY)
        return False

    def _ready_deadline(self, token):
        if self._alive(token, "ready_deadline") and self.state == FireState.PREPARING:
            self._fail(token, "not_ready_before_admission")      # nothing audible, nothing clipped

    def _admission(self, token):
        if self._alive(token, "admission") and self.state == FireState.READY:
            self._mark(FireState.ADMITTED)

    def _first_audio_deadline(self, token):
        if self._alive(token, "first_audio_deadline") and self.state in (FireState.PREPARING,):
            self._fail(token, "no_first_audio")

    def _completion_deadline(self, token):
        if self._alive(token, "completion_deadline"):
            self._fail(token, "no_completion")                    # also bounds a branch that never drains

    def _on_eos(self, token):
        if not self._alive(token, "eos"):
            return False
        if self.state in (FireState.READY, FireState.ADMITTED):
            if self.state == FireState.READY and self._now_ns() < self.admission_ns:
                # A short file can reach EOS while still waiting for admission.
                pass
            self._mark(FireState.DRAINING)
            last = self.last_end_ns
            # EOS is seen when the last buffer is QUEUED in the aggregator,
            # not rendered: retire only after the last sample (F14).
            due = max(self._now_ns(), (last or 0)) + self.DRAIN_GUARD_NS
            self._schedule(due, self._drained, token)
        return False

    def _drained(self, token):
        if self._alive(token, "drained") and self.state == FireState.DRAINING:
            self._terminal(token, FireState.ENDED, "eos_drained", fade_ms=0)

    def handle_bus_error(self, message) -> bool:
        """Called by the engine's bus router. True when this fire owns the
        source (the error is then consumed here)."""
        if self.bin is None or not self.owns(message.src):
            return False
        try:
            err = message.parse_error()[0].message
        except Exception:
            err = "error"
        GLib.idle_add(self._error, (self.fire_id, self.serial), err)
        return True

    def _error(self, token, err):
        if self._alive(token, "bus_error"):
            self._fail(token, f"bus_error:{err}")
        return False

    # -- terminal transitions ----------------------------------------------------
    def _fail(self, token, reason):
        self._terminal(token, FireState.FAILED, reason, fade_ms=0)

    def refuse(self, reason) -> bool:
        """Refuse a PLANNED fire before anything is built (owner admission
        control, e.g. the teardown system cannot safely retire a branch)."""
        if self.state != FireState.PLANNED:
            return False
        self.serial += 1
        self._mark(FireState.FAILED, reason)
        return True

    def cancel(self, reason="cancelled", fade_ms=50) -> bool:
        """Idempotent. Before admission: detach at once (nothing aired).
        Audible: fade ``fade_ms`` on the submix pad, then detach."""
        if self.state in FireState.TERMINAL or self.state == FireState.PLANNED:
            if self.state == FireState.PLANNED:
                self.serial += 1
                self._mark(FireState.CANCELLED, reason)
                return True
            self.ignored.append(("cancel", self.state))
            return False
        audible = self._now_ns() >= self.admission_ns
        self._terminal((self.fire_id, self.serial), FireState.CANCELLED, reason,
                       fade_ms=fade_ms if audible else 0)
        return True

    def _terminal(self, token, state, reason, *, fade_ms):
        if token != (self.fire_id, self.serial) or self.state in FireState.TERMINAL:
            self.ignored.append((f"terminal:{state}", self.state))
            return
        self.serial += 1                      # every older timer is now stale
        self._cancel_timers()
        self.ended_ns = self._now_ns()
        self._mark(state, reason)
        if fade_ms and self.mixer_pad is not None:
            self._fade_then_detach(fade_ms)
        else:
            self._detach()

    def _fade_then_detach(self, fade_ms):
        steps = max(1, int(fade_ms // self.FADE_STEP_MS))
        pad = self.mixer_pad
        count = [1]
        pad.set_property("volume", max(0.0, 1.0 - 1.0 / steps))
        if steps == 1:
            self._detach()
            return

        def step():
            count[0] += 1
            if self.mixer_pad is None:
                return False
            pad.set_property("volume", max(0.0, 1.0 - count[0] / steps))
            if count[0] >= steps:
                self._detach()
                return False
            return True
        GLib.timeout_add(self.FADE_STEP_MS, step)

    def _detach(self):
        """Detach first, on the GLib thread: unlink, release the request pad
        (wakes a streaming thread holding a queued future buffer), remove the
        bin. The NULL transition is handed off; it never runs here.

        Then release what ties the retired fire to its branch: the probes and
        the decoder handler (closures over this fire) and the fire's own bin
        reference -- the submit side owns the bin for every outcome it returns
        (held until NULL is confirmed, else quarantined for the process
        lifetime). Only if the submit itself raises does the fire quarantine
        the bin; a bin is never finalized while PLAYING."""
        if self.retired or self.bin is None:
            self.retired = True
            self._release_registrations()
            return
        began = time.monotonic()
        ghost = self.bin.get_static_pad("src")
        try:
            if self.mixer_pad is not None:
                ghost.unlink(self.mixer_pad)
        except Exception as exc:
            self.teardown_errors.append(f"unlink: {exc!r}")
        try:
            if self.mixer_pad is not None:
                self.mixer.release_request_pad(self.mixer_pad)
        except Exception as exc:
            self.teardown_errors.append(f"release_request_pad: {exc!r}")
        self.mixer_pad = None
        try:
            self.pipeline.remove(self.bin)
        except Exception as exc:
            self.teardown_errors.append(f"remove: {exc!r}")
        self._release_registrations()
        self.detach_ms = (time.monotonic() - began) * 1000.0
        self.retired = True
        try:
            self.teardown_outcome = self._null_submit(self, self.bin)
        except Exception as exc:
            self.teardown_errors.append(f"null_submit: {exc!r}")
            self.teardown_outcome = "error"
            audio_recovery.quarantine("vt-fires", self.fire_id, "submit_error", self.bin,
                                      {"fire_id": self.fire_id, "error": repr(exc)})
            return
        self.bin = None                       # owned by the submit side now (see the class docstring)


class VtDuckCurve:
    """Speech-owned duck on ``vt_duck_gain.volume`` (D12).

    One InterpolationControlSource is bound once and never unbound (the
    binding stays for the element's life); plans replace its points. The
    latest owner wins; a release from any other owner is ignored, so a stale
    callback can never restore or alter the current duck.

    Points are given in pipeline running time and converted to the
    element's stream time with an offset measured from its own segment."""

    IGNORED_HISTORY = 64

    def __init__(self, volume_element, now_ns: Callable[[], int]):
        self.element = volume_element
        self._now_ns = now_ns
        self.owner = None
        # Diagnostics only (stale/no-owner releases). The curve lives as long
        # as the engine and a no-owner release happens on every speech-free
        # duck refresh, so only the most recent entries are kept.
        self.ignored = deque(maxlen=self.IGNORED_HISTORY)
        self.ignored_total = 0
        self.points = ()
        self.source = GstController.InterpolationControlSource()
        self.source.set_property("mode", GstController.InterpolationMode.LINEAR)
        self.stream_minus_running_ns = 0
        sink = volume_element.get_static_pad("sink")

        def segment_probe(_pad, info):
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.SEGMENT:
                seg = event.parse_segment()
                if seg.format == Gst.Format.TIME:
                    # stream_time(t) - running_time(t) is constant within a segment.
                    probe_t = seg.start
                    running = seg.to_running_time(Gst.Format.TIME, probe_t)
                    stream = seg.to_stream_time(Gst.Format.TIME, probe_t)
                    if running != Gst.CLOCK_TIME_NONE and stream != Gst.CLOCK_TIME_NONE:
                        self.stream_minus_running_ns = int(stream) - int(running)
            return Gst.PadProbeReturn.OK
        sink.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, segment_probe)
        current = volume_element.get_property("volume")
        self._mirror = []
        self._set_points([(0, current)])
        binding = GstController.DirectControlBinding.new_absolute(volume_element, "volume", self.source)
        volume_element.add_control_binding(binding)
        self.binding = binding

    def _stream(self, running_ns):
        return max(0, int(running_ns) + self.stream_minus_running_ns)

    def value_at(self, running_ns):
        """The curve's value at a running time (linear, from the mirrored
        points; the control source holds exactly the same points)."""
        t = self._stream(running_ns)
        pts = self._mirror
        if not pts:
            return 1.0
        if t <= pts[0][0]:
            return pts[0][1]
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t0 <= t <= t1:
                return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return pts[-1][1]

    def _set_points(self, points):
        """Replace the control source's points (stream time, gain)."""
        self.source.unset_all()
        mirror = []
        for t, g in points:
            self.source.set(t, g)
            mirror.append((t, g))
        self._mirror = sorted(mirror)

    def apply(self, owner, points):
        """Become the owner and plan ``points`` [(running_ns, gain)]. The
        curve continues from its current value at "now"; points earlier than
        now are dropped."""
        now = self._now_ns()
        current = self.value_at(now)
        self.owner = owner
        planned = [(int(t), float(g)) for t, g in points if int(t) > now]
        self.points = tuple(planned)
        self._set_points([(self._stream(now), current)] + [(self._stream(t), g) for t, g in planned])
        return True

    def release(self, owner, ramp_ns) -> bool:
        """Speech ended or was cancelled/failed: ramp back to unity from now.
        Ignored (False) for any owner other than the current one."""
        if owner != self.owner or owner is None:
            self.ignored.append(("release", owner))
            self.ignored_total += 1
            return False
        now = self._now_ns()
        current = self.value_at(now)
        self._set_points([(self._stream(now), current), (self._stream(now + max(int(ramp_ns), 1)), 1.0)])
        self.points = ((now + int(ramp_ns), 1.0),)
        self.owner = None
        return True
