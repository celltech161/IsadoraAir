"""Roadmap 2.23 Start Next -- one deck's fade on its own mixer input.

The fade drives the ``volume`` property of the outgoing deck's audiomixer
REQUEST PAD through a GstController InterpolationControlSource bound with
DirectControlBinding.new_absolute. audioaggregator applies it on its own
streaming thread (``gst_object_sync_values`` once per output buffer), so the
browser and the GLib loop never step a volume themselves and no other gain
stage (VT duck, mic duck, FX/VT submix, master/program gain) is touched.

Time domain: the controller is evaluated at whatever timestamp the
aggregator passes to ``gst_object_sync_values`` -- measured on GStreamer 1.28
to be the pad's RUNNING time (it tracked the pipeline clock to within one
10 ms buffer; the deck's media stream time was offset by its pad offset plus
the 0.3 s silence prime). Rather than assume that, the curve is anchored on
the pad's own ``GstObject.last_sync`` -- the timestamp of the aggregator's
latest sync of this pad -- read once the binding has been synced, so the
fade starts at the buffer the mixer is producing now in the domain the
mixer actually uses. If the pad never syncs (no data), the clock's running
time is the documented fallback.
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


class MixerPadFade:
    """A cancellable fade of one audiomixer request pad. GLib thread only."""

    def __init__(self, pad, pipeline):
        self.pad = pad
        self.pipeline = pipeline
        self.anchor_ns = None
        self.end_ns = None
        self.anchored_from = None    # "last_sync" | "clock"
        self.detached = False
        self._initial_sync = self._last_sync()
        self._source = GstController.InterpolationControlSource()
        self._source.set_property("mode", GstController.InterpolationMode.LINEAR)
        # An empty source yields no value, so the pad keeps volume 1.0 until
        # anchor(); the aggregator still records last_sync for the pad.
        self._binding = GstController.DirectControlBinding.new_absolute(
            pad, "volume", self._source
        )
        pad.add_control_binding(self._binding)

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

    def synced(self):
        """True once the aggregator has synced this pad since binding."""
        current = self._last_sync()
        return current is not None and current != self._initial_sync

    def anchor(self, duration_ns, *, fallback_to_clock=False):
        """Start the fade at the mixer's current position. Returns False if
        the pad has not been synced yet (call again on the next tick)."""
        if self.synced():
            start, self.anchored_from = self._last_sync(), "last_sync"
        elif fallback_to_clock and self._clock_running_ns() is not None:
            start, self.anchored_from = self._clock_running_ns(), "clock"
        else:
            return False
        self.anchor_ns = start
        self.end_ns = start + int(duration_ns)
        self._source.unset_all()
        for index in range(FADE_CONTROL_POINTS):
            fraction = index / (FADE_CONTROL_POINTS - 1)
            self._source.set(start + int(duration_ns * fraction), fade_curve(fraction))
        return True

    def restore(self, duration_ns):
        """Ramp back to unity from wherever the fade is now. Returns False
        (and resets the pad to unity at once) if it never began."""
        start = self._last_sync() if self.synced() else None
        if start is None or self.anchor_ns is None:
            self.detach(volume=1.0)
            return False
        current = self.current_volume()
        self._source.unset_all()
        self._source.set(start, current)
        self._source.set(start + int(duration_ns), 1.0)
        return True

    def current_volume(self):
        """The value the aggregator last synced onto the pad."""
        try:
            return float(self.pad.get_property("volume"))
        except Exception:
            return 1.0

    def detach(self, *, volume=None):
        """Stop driving the pad by DISABLING the binding. ``volume`` (e.g. 1.0
        after a restore) is then set directly; None leaves the last synced
        value (used right before the pad is released, so a faded deck stays
        silent while it is retired).

        The binding is deliberately never removed while the pad may still be
        in a running mixer: gst_object_sync_values() walks a pad's binding
        list WITHOUT the object lock ("FIXME: this deadlocks" upstream), so
        gst_object_remove_control_binding() from the GLib thread races the
        aggregator thread into a use-after-free (reproduced as a SIGSEGV in
        gst_object_sync_values during development). Disabling only flips a
        flag that sync_values reads; the disabled binding is released with
        the pad when the deck generation retires."""
        if self.detached:
            return
        self.detached = True
        try:
            self._binding.set_disabled(True)
        except Exception:
            pass
        if volume is not None:
            try:
                self.pad.set_property("volume", float(volume))
            except Exception:
                pass
