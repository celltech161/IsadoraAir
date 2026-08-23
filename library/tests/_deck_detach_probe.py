"""Subprocess-only real-GStreamer probe for wedged deck detach operations."""

from __future__ import annotations

import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


def main(operation):
    Gst.init(None)
    pipeline = Gst.Pipeline.new("detach-probe")
    mixer = Gst.ElementFactory.make("audiomixer", "mixer")
    sink = Gst.ElementFactory.make("fakesink", "sink")
    sink.set_property("sync", False)
    deck_bin = Gst.Bin.new("wedged-deck")
    source = Gst.ElementFactory.make("audiotestsrc", "source")
    source.set_property("is-live", True)
    deck_bin.add(source)
    ghost = Gst.GhostPad.new("src", source.get_static_pad("src"))
    deck_bin.add_pad(ghost)
    pipeline.add(mixer)
    pipeline.add(sink)
    pipeline.add(deck_bin)
    mixer.link(sink)
    mixer_pad = mixer.request_pad_simple("sink_%u")
    ghost.link(mixer_pad)

    entered = threading.Event()
    release = threading.Event()

    def wedge(_pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            entered.set()
            release.wait(timeout=10)
        return Gst.PadProbeReturn.OK

    source.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, wedge)
    pipeline.set_state(Gst.State.PLAYING)
    if not entered.wait(timeout=2):
        return 3

    started = time.monotonic()
    if operation == "unlink":
        ghost.unlink(mixer_pad)
    elif operation == "release":
        ghost.unlink(mixer_pad)
        mixer.release_request_pad(mixer_pad)
    elif operation == "remove":
        ghost.unlink(mixer_pad)
        mixer.release_request_pad(mixer_pad)
        pipeline.remove(deck_bin)
    elif operation == "null":
        ghost.unlink(mixer_pad)
        mixer.release_request_pad(mixer_pad)
        pipeline.remove(deck_bin)
        deck_bin.set_state(Gst.State.NULL)
    else:
        return 2
    print(f"{operation} {time.monotonic() - started:.6f}", flush=True)
    release.set()
    if operation == "remove":
        deck_bin.set_state(Gst.State.NULL)
    pipeline.set_state(Gst.State.NULL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
