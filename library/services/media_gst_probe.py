#!/usr/bin/env python3
"""Isolated GStreamer decode probe used by media_health.

This process intentionally has no Django imports and prints exactly one small
JSON result.  The parent supplies an additional hard process-group timeout;
the local deadline prevents a healthy GStreamer loop from lingering.
"""

import argparse
import json
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


def _bounded(value, limit=4096):
    text = str(value or "")
    return text[:limit]


def probe(path, timeout_seconds):
    Gst.init(None)
    pipeline = Gst.Pipeline.new("isolated-media-validator")
    source = Gst.ElementFactory.make("filesrc", "source")
    decoder = Gst.ElementFactory.make("decodebin", "decoder")
    convert = Gst.ElementFactory.make("audioconvert", "convert")
    resample = Gst.ElementFactory.make("audioresample", "resample")
    capsfilter = Gst.ElementFactory.make("capsfilter", "raw-audio")
    sink = Gst.ElementFactory.make("fakesink", "sink")
    elements = (source, decoder, convert, resample, capsfilter, sink)
    if not pipeline or any(element is None for element in elements):
        return {"status": "infrastructure_error", "error": "required GStreamer element unavailable"}

    source.set_property("location", path)
    capsfilter.set_property("caps", Gst.Caps.from_string("audio/x-raw"))
    sink.set_property("sync", False)
    for element in elements:
        pipeline.add(element)
    if not source.link(decoder) or not convert.link(resample) or not resample.link(capsfilter) or not capsfilter.link(sink):
        pipeline.set_state(Gst.State.NULL)
        return {"status": "infrastructure_error", "error": "failed to link validator topology"}

    linked_audio = {"value": False}
    buffers = {"count": 0}

    def on_pad_added(_decoder, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        structure = caps.get_structure(0) if caps and caps.get_size() else None
        if structure and structure.get_name().startswith("audio/"):
            sink_pad = convert.get_static_pad("sink")
            if not sink_pad.is_linked() and pad.link(sink_pad) == Gst.PadLinkReturn.OK:
                linked_audio["value"] = True

    def count_buffer(_pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            buffers["count"] += 1
        return Gst.PadProbeReturn.OK

    decoder.connect("pad-added", on_pad_added)
    sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, count_buffer)
    bus = pipeline.get_bus()
    started = time.monotonic()
    result = {"status": "timeout", "eos": False}
    try:
        change = pipeline.set_state(Gst.State.PLAYING)
        if change == Gst.StateChangeReturn.FAILURE:
            result = {"status": "error", "eos": False, "error": "pipeline failed to enter PLAYING"}
        else:
            mask = Gst.MessageType.ERROR | Gst.MessageType.EOS
            while time.monotonic() - started < timeout_seconds:
                message = bus.timed_pop_filtered(250 * Gst.MSECOND, mask)
                if message is None:
                    continue
                if message.type == Gst.MessageType.EOS:
                    result = {"status": "eos", "eos": True}
                    break
                error, debug = message.parse_error()
                result = {
                    "status": "error",
                    "eos": False,
                    "error": _bounded(error),
                    "debug": _bounded(debug),
                }
                break
    finally:
        pipeline.set_state(Gst.State.NULL)

    result.update({
        "audio_pad_linked": linked_audio["value"],
        "buffers": buffers["count"],
        "duration_seconds": round(time.monotonic() - started, 3),
    })
    if result["status"] == "eos" and (not linked_audio["value"] or buffers["count"] == 0):
        result["status"] = "error"
        result["eos"] = False
        result["error"] = "no decoded audio buffers"
    return result


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("path")
    args = parser.parse_args()
    try:
        result = probe(args.path, max(0.1, args.timeout))
    except Exception as exc:  # process boundary: always return structured evidence
        result = {"status": "infrastructure_error", "error": _bounded(exc)}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
