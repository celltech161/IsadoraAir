"""Roadmap 2.23 Start Next -- the ONE volume controller of a deck's mixer input.

The fade drives the ``volume`` property of the outgoing deck's audiomixer
REQUEST PAD through a GstController InterpolationControlSource bound with
DirectControlBinding.new_absolute. audioaggregator applies it on its own
streaming thread (``gst_object_sync_values`` once per output buffer), so the
browser and the GLib loop never step a volume themselves and no other gain
stage (VT duck, mic duck, FX/VT submix, master/program gain) is touched.

Binding lifetime (r0117 review SN-GST-1). ``gst_object_sync_values()`` walks
a pad's binding list WITHOUT the object lock ("FIXME: this deadlocks"
upstream). Removing a binding, or adding a second one for the same property
(which REPLACES -- i.e. removes -- the first), while the aggregator syncs that
pad is a use-after-free: both reproduced as SIGSEGV in isolated GStreamer
1.28 probes (evidence/track-a/gstreamer). Therefore:

* a pad gets at most ONE binding for ``volume``, created the first time a
  fade needs it and refused if the pad already carries any volume binding;
* the binding is never removed, replaced or disabled while the pad lives --
  every fade, cancellation and restoration only reprograms its control
  source (internally locked, safe against the streaming thread);
* the binding is released only with the pad itself: the engine unlinks and
  releases the request pad (deck retirement or Pause) and drops its
  reference; the aggregator's sink-pad iteration holds pad references, so
  the pad -- and with it the binding -- is finalized only once no sync is in
  progress.

Time domain: the controller is evaluated at whatever timestamp the aggregator
passes to ``gst_object_sync_values`` -- measured on GStreamer 1.28 to be the
pad's RUNNING time. Curves are anchored on the pad's own
``GstObject.last_sync`` (the timestamp of the aggregator's latest sync of this
pad) once it has advanced past the value seen when the fade was armed, so a
fade starts at the buffer the mixer is producing now, in the domain the mixer
actually uses. If the pad never syncs (no data), the clock's running time is
the documented fallback.

Every method runs on the GLib thread.
"""

from __future__ import annotations

import math

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

try:
    gi.require_version("GstController", "1.0")
    from gi.repository import GstController
except (ImportError, ValueError):  # pragma: no cover - typelib ships with gir1.2-gstreamer-1.0
    GstController = None


# Equal-power (quarter-cosine) fade-out sampled at this many linear control
# points: smooth at the start, reaches exactly 0.0 at the end.
FADE_CONTROL_POINTS = 33


class ControlConflict(RuntimeError):
    """The pad already carries a volume binding this module does not own;
    binding it again would replace that binding under the streaming thread."""


def fade_supported():
    """GstController's typelib is part of gir1.2-gstreamer-1.0 (the package
    that provides Gst itself); this guards an incomplete GI install."""
    return GstController is not None


def fade_curve(fraction):
    """Gain at ``fraction`` (0..1) of the fade."""
    fraction = min(1.0, max(0.0, fraction))
    if fraction >= 1.0:
        return 0.0
    return math.cos(math.pi / 2.0 * fraction)


def _valid_time(value):
    return isinstance(value, int) and 0 <= value < Gst.CLOCK_TIME_NONE


class PadVolumeControl:
    """The single, persistent volume controller of one audiomixer request pad.

    Created lazily (the first Start Next on that deck generation) and reused
    for every later fade/cancel/restore on the same pad; it is never detached.
    The deck drops it when the pad is released."""

    def __init__(self, pad, pipeline):
        if pad.get_control_binding("volume") is not None:
            raise ControlConflict("pad already has a volume control binding")
        self.pad = pad
        self.pipeline = pipeline
        self.anchor_ns = None
        self.end_ns = None
        self.anchored_from = None    # "last_sync" | "clock"
        self._armed_sync = None
        self._source = GstController.InterpolationControlSource()
        self._source.set_property("mode", GstController.InterpolationMode.LINEAR)
        # An empty source yields no value, so the pad keeps its current volume
        # until the first program; the aggregator still records last_sync.
        self.binding = GstController.DirectControlBinding.new_absolute(
            pad, "volume", self._source
        )
        if not pad.add_control_binding(self.binding):
            raise ControlConflict("add_control_binding refused the volume binding")

    # -- observation -------------------------------------------------------
    def _last_sync(self):
        try:
            value = self.pad.last_sync
        except Exception:
            return None
        return value if _valid_time(value) else None

    def _clock_running_ns(self):
        clock = self.pipeline.get_clock()
        if clock is None:
            return None
        return clock.get_time() - self.pipeline.get_base_time()

    def attached_bindings(self):
        """The pad's actual attached binding list length (not 'active' ones)."""
        try:
            return len(self.pad.control_bindings)
        except Exception:
            return None

    def owns_pad_binding(self):
        return self.pad.get_control_binding("volume") is self.binding

    def current_volume(self):
        """The value the aggregator last synced onto the pad."""
        try:
            return float(self.pad.get_property("volume"))
        except Exception:
            return 1.0

    def _now(self):
        """The mixer's current position in the sync domain, for reprogramming
        an already-running control (the pad syncs every output buffer)."""
        start = self._last_sync()
        return start if start is not None else self._clock_running_ns()

    # -- programming -------------------------------------------------------
    def arm(self):
        """Begin a fade request: remember last_sync so anchor_fade() waits for
        a sync that happened after this moment."""
        self._armed_sync = self._last_sync()
        self.anchor_ns = None
        self.end_ns = None
        self.anchored_from = None

    def anchor_fade(self, duration_ns, *, fallback_to_clock=False):
        """Program the equal-power fade from the current gain to silence,
        starting at the mixer's current position. Returns False (and changes
        nothing) until the pad has been synced since arm()."""
        current = self._last_sync()
        if current is not None and current != self._armed_sync:
            start, self.anchored_from = current, "last_sync"
        elif fallback_to_clock and self._clock_running_ns() is not None:
            start, self.anchored_from = self._clock_running_ns(), "clock"
        else:
            return False
        gain = self.current_volume()
        self.anchor_ns = start
        self.end_ns = start + int(duration_ns)
        self._source.unset_all()
        for index in range(FADE_CONTROL_POINTS):
            fraction = index / (FADE_CONTROL_POINTS - 1)
            self._source.set(start + int(duration_ns * fraction), gain * fade_curve(fraction))
        return True

    def ramp_to_unity(self, duration_ns):
        """Restore: a linear ramp from the current gain back to 1.0, which the
        source then holds for the pad's remaining life."""
        start = self._now()
        if start is None:
            return False
        self._source.unset_all()
        self._source.set(start, self.current_volume())
        self._source.set(start + int(duration_ns), 1.0)
        self.anchor_ns = None
        self.end_ns = None
        return True

    def control_points(self):
        """(time, value) pairs currently programmed -- for tests and evidence."""
        try:
            return [(point.timestamp, point.value) for point in self._source.get_all()]
        except Exception:
            return []
