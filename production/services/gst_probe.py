#!/usr/bin/env python3
"""Isolated GStreamer decode probe for production-media validation.

Run as a child process (no Django). Decodes the file through the same
``filesrc ! decodebin ! audioconvert ! audioresample ! fakesink`` topology the
playout engine relies on and prints exactly one bounded JSON object.

Its whole job is to tell two things apart, using structured GStreamer evidence
(GError domain + code, missing-plugin messages, element-factory failures) and
never localized message text:

* ``media_error`` -- the runtime is capable but these BYTES do not decode:
  STREAM errors DECODE / DEMUX / FORMAT / WRONG_TYPE / DECRYPT /
  DECRYPT_NOKEY, or an audio pad that produced no buffers at all.
* ``capability_error`` -- the STATION RUNTIME cannot do the job: GI/GStreamer
  missing, a required element missing, a missing-plugin message, CORE
  MISSING_PLUGIN / NEGOTIATION, STREAM CODEC_NOT_FOUND / TYPE_NOT_FOUND /
  NOT_IMPLEMENTED, or no audio pad at all. The input has already passed the
  allowlist, ffprobe and a full ffmpeg decode, so a GStreamer that cannot
  even find a decoder for an allowlisted codec is a runtime problem, not
  corrupt bytes.
* ``infrastructure_error`` -- anything else ambiguous (RESOURCE/LIBRARY errors,
  generic STREAM FAILED, an unexpected exception). Never a media verdict.
* ``timeout`` / ``eos``.

The parent (production.services.validation) maps only ``media_error`` to an
``invalid`` verdict; every other non-``eos`` outcome is a retryable
infrastructure failure.
"""
import argparse
import json
import time

_LIMIT = 200
_MAX_MISSING = 8


def _bounded(value, limit=_LIMIT):
    return str(value or "")[:limit]


def _result(status, reason, **extra):
    out = {"status": status, "reason": reason}
    out.update(extra)
    return out


def _classify_error(Gst, error, saw_missing_plugin):
    domain, code = error.domain, error.code
    evidence = {"error_domain": _bounded(domain, 64), "error_code": int(code)}
    if saw_missing_plugin:
        return _result("capability_error", "missing_plugin", **evidence)
    if error.matches(Gst.CoreError.quark(), Gst.CoreError.MISSING_PLUGIN):
        return _result("capability_error", "missing_plugin", **evidence)
    if error.matches(Gst.CoreError.quark(), Gst.CoreError.NEGOTIATION):
        return _result("capability_error", "negotiation", **evidence)
    stream = Gst.StreamError
    for capability in (stream.CODEC_NOT_FOUND, stream.TYPE_NOT_FOUND, stream.NOT_IMPLEMENTED):
        if error.matches(stream.quark(), capability):
            return _result("capability_error", "stream_capability", **evidence)
    for undecodable in (stream.DECODE, stream.DEMUX, stream.FORMAT, stream.WRONG_TYPE,
                        stream.DECRYPT, stream.DECRYPT_NOKEY):
        if error.matches(stream.quark(), undecodable):
            return _result("media_error", "stream_undecodable", **evidence)
    return _result("infrastructure_error", "unclassified_error", **evidence)


def probe(path, timeout_seconds):
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except Exception as exc:  # noqa: BLE001 -- no GI/GStreamer at all
        return _result("capability_error", "gi_unavailable", detail=_bounded(type(exc).__name__))
    try:
        gi.require_version("GstPbutils", "1.0")
        from gi.repository import GstPbutils
    except Exception:  # noqa: BLE001 -- missing-plugin detection degrades to error codes
        GstPbutils = None
    try:
        Gst.init(None)
    except Exception as exc:  # noqa: BLE001
        return _result("capability_error", "gst_init_failed", detail=_bounded(type(exc).__name__))

    names = ("filesrc", "decodebin", "audioconvert", "audioresample", "capsfilter", "fakesink")
    elements = {}
    for name in names:
        try:
            element = Gst.ElementFactory.make(name, None)
        except Exception:  # noqa: BLE001 -- newer overrides raise MissingPluginError
            element = None
        if element is None:
            return _result("capability_error", "element_unavailable", element=name)
        elements[name] = element

    pipeline = Gst.Pipeline.new("production-media-validator")
    source, decoder, convert, resample, capsfilter, sink = (elements[name] for name in names)
    source.set_property("location", path)
    capsfilter.set_property("caps", Gst.Caps.from_string("audio/x-raw"))
    sink.set_property("sync", False)
    for element in elements.values():
        pipeline.add(element)
    if not (source.link(decoder) and convert.link(resample) and resample.link(capsfilter)
            and capsfilter.link(sink)):
        pipeline.set_state(Gst.State.NULL)
        return _result("capability_error", "link_failed")

    linked_audio = {"value": False}
    buffers = {"count": 0}
    missing = []

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
    result = _result("timeout", "deadline")
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            # Let a posted error/missing-plugin message explain the failure.
            result = _result("infrastructure_error", "state_change_failed")
        mask = Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.ELEMENT
        while time.monotonic() - started < timeout_seconds:
            message = bus.timed_pop_filtered(250 * Gst.MSECOND, mask)
            if message is None:
                if result["status"] == "infrastructure_error":
                    break
                continue
            if message.type == Gst.MessageType.ELEMENT:
                if GstPbutils is not None and GstPbutils.is_missing_plugin_message(message):
                    if len(missing) < _MAX_MISSING:
                        missing.append(_bounded(GstPbutils.missing_plugin_message_get_description(message)))
                continue
            if message.type == Gst.MessageType.EOS:
                result = _result("eos", "end_of_stream")
                break
            error, _debug = message.parse_error()
            result = _classify_error(Gst, error, bool(missing))
            break
    finally:
        pipeline.set_state(Gst.State.NULL)

    if missing and result["status"] != "eos":
        result["status"], result["reason"] = "capability_error", "missing_plugin"
    if result["status"] == "eos":
        if not linked_audio["value"]:
            result = _result("capability_error", "no_audio_pad")
        elif buffers["count"] == 0:
            result = _result("media_error", "no_decoded_audio")
    result.update({
        "audio_pad_linked": linked_audio["value"],
        "buffers": buffers["count"],
        "missing_plugins": missing,
        "duration_seconds": round(time.monotonic() - started, 3),
    })
    return result


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("path")
    args = parser.parse_args()
    try:
        result = probe(args.path, max(0.1, args.timeout))
    except Exception as exc:  # noqa: BLE001 -- process boundary: always structured evidence
        result = _result("infrastructure_error", "probe_exception", detail=_bounded(type(exc).__name__))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
