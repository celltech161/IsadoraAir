#!/usr/bin/env python3
"""Boundary-level Studio Monitor retarget harness.

Safe default: audiotestsrc + fakesinks only. It never writes engine IPC,
changes a service, opens ALSA, or queries/saves a Django model. Production
OutputRecoverySlot methods are exercised through the existing real-GStreamer
test helpers.

Measured path:
  tee -> P1 queue:sink -> queue -> P2 queue:src -> errorignore -> valve
      -> P3 valve:src -> audiodynamic -> volume -> rglimiter
      -> P4 limiter:src -> sink

Optional P5 independent ALSA capture is available only with an explicit
--physical configuration. The operator must provide a real physical loop from
each named output to its independent capture input.

Probe callbacks do no logging and inspect at most 256 F32LE samples per buffer.
All other telemetry is sampled on a background thread.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scratchpad" / "audio_output_recovery"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "isadoraair.settings")

import django  # noqa: E402

django.setup()

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from _instrumentation import BusRecorder, PeriodicSampler, dump_json, wait_until  # noqa: E402
from library.services import audio_recovery  # noqa: E402
import library.services.engine as engine  # noqa: E402
from library.tests.test_engine_output_recovery import (  # noqa: E402
    fake_error_message,
    make_output_engine_stand_in,
)

Gst.init(None)
SAMPLE_LIMIT = 256
SIBLING_GAP_LIMIT_S = 0.15


class PCMProbe:
    """Constant-space, bounded-work statistics for forced F32LE PCM."""

    def __init__(self, label):
        self.label = label
        self.buffers = 0
        self.nonzero = 0
        self.all_zero = 0
        self.samples = 0
        self.sum_abs = 0.0
        self.sum_sq = 0.0
        self.peak = 0.0
        self.first_ts = None
        self.last_ts = None
        self.max_gap = 0.0
        self.caps = None
        self.map_failures = 0
        self.unsupported_caps = 0
        self.lock = threading.Lock()

    def callback(self, pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        now = time.monotonic()
        caps = pad.get_current_caps()
        caps_text = caps.to_string() if caps is not None else None
        fmt = (
            caps.get_structure(0).get_string("format")
            if caps is not None and caps.get_size() else None
        )
        n = 0
        sum_abs = sum_sq = peak = 0.0
        raw_nonzero = None
        ok, mapped = buf.map(Gst.MapFlags.READ)
        if ok:
            # Exact whole-buffer zero verdict. RMS/mean/peak below remain
            # deliberately bounded to SAMPLE_LIMIT values.
            raw_nonzero = any(mapped.data)
        if ok and fmt == "F32LE":
            try:
                total = len(mapped.data) // 4
                stride = max(1, total // SAMPLE_LIMIT) if total else 1
                for index in range(0, total, stride):
                    value = struct.unpack_from("<f", mapped.data, index * 4)[0]
                    absolute = abs(value)
                    n += 1
                    sum_abs += absolute
                    sum_sq += value * value
                    peak = max(peak, absolute)
                    if n >= SAMPLE_LIMIT:
                        break
            finally:
                buf.unmap(mapped)
        elif ok:
            buf.unmap(mapped)

        with self.lock:
            self.buffers += 1
            if self.first_ts is None:
                self.first_ts = now
            if self.last_ts is not None:
                self.max_gap = max(self.max_gap, now - self.last_ts)
            self.last_ts = now
            self.caps = caps_text or self.caps
            if not ok:
                self.map_failures += 1
            elif fmt != "F32LE":
                self.unsupported_caps += 1
            if raw_nonzero is not None:
                if raw_nonzero:
                    self.nonzero += 1
                else:
                    self.all_zero += 1
            if n:
                self.samples += n
                self.sum_abs += sum_abs
                self.sum_sq += sum_sq
                self.peak = max(self.peak, peak)
        return Gst.PadProbeReturn.OK

    def snapshot(self):
        with self.lock:
            n = self.samples
            return {
                "label": self.label,
                "buffer_count": self.buffers,
                "nonzero_buffers": self.nonzero,
                "all_zero_buffers": self.all_zero,
                "sample_count": n,
                # Retain cumulative energy totals so a caller can calculate
                # statistics for a bounded phase without resetting a probe on
                # the streaming thread.
                "sample_abs_sum": self.sum_abs,
                "sample_square_sum": self.sum_sq,
                "mean_abs": self.sum_abs / n if n else None,
                "rms": math.sqrt(self.sum_sq / n) if n else None,
                "peak": self.peak,
                "first_buffer_ts": self.first_ts,
                "last_buffer_ts": self.last_ts,
                "max_inter_buffer_gap_s": self.max_gap,
                "caps": self.caps,
                "map_failures": self.map_failures,
                "unsupported_caps": self.unsupported_caps,
            }


def attach(pad, label):
    probe = PCMProbe(label)
    pad.add_probe(Gst.PadProbeType.BUFFER, probe.callback)
    return probe


class QueueSignals:
    """Queue exposes no numeric drop count; record its available signals."""

    def __init__(self, queue):
        self.values = {key: 0 for key in ("overrun", "underrun", "running", "pushing")}
        self.lock = threading.Lock()
        for key in self.values:
            queue.connect(key, self._event, key)

    def _event(self, queue, key):
        with self.lock:
            self.values[key] += 1

    def snapshot(self):
        with self.lock:
            return dict(self.values)


class P5Capture:
    """Independent alsasrc path; constructed only in explicit physical mode."""

    def __init__(self, identity, device):
        self.identity = identity
        self.pipeline = Gst.Pipeline.new(f"p5-{identity.lower()}")
        src = Gst.ElementFactory.make("alsasrc", None)
        convert = Gst.ElementFactory.make("audioconvert", None)
        caps = Gst.ElementFactory.make("capsfilter", None)
        sink = Gst.ElementFactory.make("fakesink", None)
        if not all((src, convert, caps, sink)):
            raise RuntimeError("P5 capture elements unavailable")
        src.set_property("device", device)
        caps.set_property("caps", Gst.Caps.from_string(
            "audio/x-raw,format=F32LE,rate=48000,channels=2,layout=interleaved"))
        sink.set_property("sync", False)
        sink.set_property("async", False)
        for element in (src, convert, caps, sink):
            self.pipeline.add(element)
        if not src.link(convert) or not convert.link(caps) or not caps.link(sink):
            raise RuntimeError(f"could not link P5 capture for {identity}")
        self.probe = attach(sink.get_static_pad("sink"), f"P5_{identity}_physical_capture")

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"P5 {self.identity} failed to start")
        if not wait_until(lambda: self.pipeline.get_state(0)[1] == Gst.State.PLAYING, 5.0):
            raise RuntimeError(f"P5 {self.identity} did not reach PLAYING")

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)


class SinkView:
    def __init__(self, sink, device):
        self.sink, self.device = sink, device

    def get_property(self, name):
        return self.device if name == "device" else self.sink.get_property(name)

    def get_static_pad(self, name):
        return self.sink.get_static_pad(name)


class StudioGenerationFactory:
    def __init__(self, rig):
        self.rig = rig
        self.fail = set()
        self.calls = []
        self.generations = {}
        self.serial = 0

    def __call__(self, runtime_device):
        self.serial += 1
        self.calls.append(runtime_device)
        failed = runtime_device in self.fail
        if self.rig.args.physical and not failed:
            actual = self.rig.sink_map.get(runtime_device)
            if not actual:
                raise RuntimeError(f"no physical sink mapping for {runtime_device}")
            bin_, raw_sink = self.rig.obj._build_studio_monitor_hw_generation(actual)
            if self.rig.args.physical_sink_sync_true:
                raw_sink.set_property("sync", True)
            sink = raw_sink
        else:
            bin_, raw_sink = self.rig.obj._build_studio_monitor_hw_generation(
                None, sink_factory="fakesink")
            raw_sink.set_property("sync", False)
            sink = SinkView(raw_sink, runtime_device)

        limiter = bin_.get_by_name("agc_limiter")
        if failed:
            # P4 remains observable, but no buffer reaches GstBaseSink;
            # rendered-count verification therefore fails deterministically.
            limiter.unlink(raw_sink)
            gate = Gst.ElementFactory.make("valve", f"failed_open_{self.serial}")
            gate.set_property("drop", True)
            bin_.add(gate)
            limiter.link(gate)
            gate.link(raw_sink)

        meta = {
            "serial": self.serial,
            "device": runtime_device,
            "failed": failed,
            "P4": attach(limiter.get_static_pad("src"),
                         f"P4_gen{self.serial}_{runtime_device}"),
            "sink_input": attach(raw_sink.get_static_pad("sink"),
                                 f"sink_input_gen{self.serial}_{runtime_device}"),
            "sink": sink,
        }
        self.generations[bin_] = meta
        return bin_, sink

    def current(self, slot):
        return self.generations.get(slot.current_bin)


class Harness:
    def __init__(self, args):
        self.args = args
        self.identities = {
            "Studio Monitor": args.initial_identity,
            "Stereotool Input": "Loopback",
        }
        self.obj = make_output_engine_stand_in()
        self.obj.running = True
        self.obj._studio_monitor_agc_settings = {
            "enabled": True, "ratio": 10.0, "threshold": 0.9,
            "soft_knee": True, "makeup_gain_db": 0.0,
        }
        self.obj._resolve_output_device_identity = (
            lambda name: ("alsa_card_id", self.identities[name])
        )
        self.sink_map = {
            "plughw:CARD=PCH,DEV=0": args.sink_a,
            "plughw:CARD=CODEC,DEV=0": args.sink_b,
        } if args.physical else {}
        self.factory = StudioGenerationFactory(self)
        self.pipeline = Gst.Pipeline.new("studio-boundary-harness")
        self.obj.main_pipeline = self.pipeline
        self.monitor = self.obj._build_output_slot(
            "Studio Monitor", "studio_monitor", "synthetic-fallback", self.factory)
        self.stereo = self.obj._build_output_slot(
            "Stereotool Input", "stereotool", "synthetic-stereo",
            self._sibling_generation)
        self.obj._studio_monitor_slot = self.monitor
        self.obj._stereotool_slot = self.stereo
        self.obj._output_slots = {"studio_monitor": self.monitor, "stereotool": self.stereo}

        src = Gst.ElementFactory.make("audiotestsrc", "program_tone")
        convert = Gst.ElementFactory.make("audioconvert", None)
        caps = Gst.ElementFactory.make("capsfilter", None)
        self.tee = Gst.ElementFactory.make("tee", "program_tee")
        src.set_property("is-live", True)
        src.set_property("volume", 0.25)
        src.set_property("samplesperbuffer", 1024)
        caps.set_property("caps", Gst.Caps.from_string(
            "audio/x-raw,format=F32LE,rate=48000,channels=2,layout=interleaved"))
        for element in (
                src, convert, caps, self.tee,
                self.monitor.queue, self.monitor.errorignore, self.monitor.valve,
                self.monitor.current_bin, self.stereo.queue, self.stereo.errorignore,
                self.stereo.valve, self.stereo.current_bin):
            self.pipeline.add(element)
        if not src.link(convert) or not convert.link(caps) or not caps.link(self.tee):
            raise RuntimeError("shared path link failed")
        for slot in (self.monitor, self.stereo):
            if not self.tee.link(slot.queue):
                raise RuntimeError(f"{slot.name} tee link failed")
            if not slot.queue.link(slot.errorignore) or not slot.errorignore.link(slot.valve):
                raise RuntimeError(f"{slot.name} containment link failed")
            if slot.valve.get_static_pad("src").link(
                    slot.current_bin.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
                raise RuntimeError(f"{slot.name} generation link failed")

        self.P1 = attach(self.monitor.queue.get_static_pad("sink"), "P1_queue_input")
        self.P2 = attach(self.monitor.queue.get_static_pad("src"), "P2_queue_output")
        self.P3 = attach(self.monitor.valve.get_static_pad("src"), "P3_valve_output")
        self.sibling = attach(self.stereo.current_sink.get_static_pad("sink"),
                              "StereoTool_sink_input")
        self.queue_signals = QueueSignals(self.monitor.queue)
        self.bus = BusRecorder()
        self.pipeline.get_bus().set_sync_handler(self._bus_sync, None)
        self.captures = {}
        if args.physical:
            if args.capture_a:
                self.captures["PCH"] = P5Capture("PCH", args.capture_a)
            if args.capture_b:
                self.captures["CODEC"] = P5Capture("CODEC", args.capture_b)
        self.started_at = None
        self.telemetry = PeriodicSampler(self._telemetry, interval_s=0.02)
        self.phases = []

    def _sibling_generation(self, device):
        bin_ = Gst.Bin.new(f"stereo_gen_{time.monotonic_ns()}")
        identity = Gst.ElementFactory.make("identity", None)
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        bin_.add(identity)
        bin_.add(sink)
        identity.link(sink)
        ghost = Gst.GhostPad.new("sink", identity.get_static_pad("sink"))
        ghost.set_active(True)
        bin_.add_pad(ghost)
        return bin_, sink

    def _bus_sync(self, bus, message, data):
        self.bus.on_message(bus, message)
        return Gst.BusSyncReply.PASS

    @staticmethod
    def _hw_ptr(path):
        if not path:
            return None
        try:
            for line in Path(path).read_text().splitlines():
                if line.startswith("hw_ptr"):
                    return int(line.split(":", 1)[1].strip())
        except (OSError, ValueError):
            return None
        return None

    def _telemetry(self):
        state = self.monitor.coordinator.snapshot()
        current = self.factory.current(self.monitor)
        rendered = (
            engine._output_sink_rendered_count(self.monitor.current_sink)
            if self.monitor.current_sink is not None else None
        )
        return {
            "pipeline_state": self.pipeline.get_state(0)[1].value_nick,
            "stereotool_state": self.stereo.current_bin.get_state(0)[1].value_nick,
            "queue_buffers": self.monitor.queue.get_property("current-level-buffers"),
            "queue_bytes": self.monitor.queue.get_property("current-level-bytes"),
            "queue_time": self.monitor.queue.get_property("current-level-time"),
            "queue_leaky": self.monitor.queue.get_property("leaky").value_nick,
            "valve_drop": self.monitor.valve.get_property("drop"),
            "generation": state["generation"],
            "ownership_epoch": self.monitor.device_loss_epoch(),
            "slot_state": state["state"],
            "operation_state": state["operation_state"],
            "retarget_requested": self.monitor.retarget_requested,
            "retarget_in_progress": self.monitor.retarget_in_progress,
            "current_serial": current["serial"] if current else None,
            "current_device": current["device"] if current else None,
            "sink_rendered": rendered,
            "hw_ptr_pch": self._hw_ptr(self.args.hw_status_a),
            "hw_ptr_codec": self._hw_ptr(self.args.hw_status_b),
        }

    def start(self):
        for capture in self.captures.values():
            capture.start()
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise AssertionError("pipeline start failed")
        if not wait_until(lambda: self.pipeline.get_state(0)[1] == Gst.State.PLAYING, 5):
            raise AssertionError("pipeline never reached PLAYING")
        if not wait_until(lambda: self.sibling.snapshot()["buffer_count"] >= 8, 3):
            raise AssertionError("StereoTool never started")
        self.started_at = time.monotonic()
        self.telemetry.start()

    def stop(self):
        self.telemetry.stop()
        self.pipeline.set_state(Gst.State.NULL)
        for capture in self.captures.values():
            capture.stop()

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.obj._output_recovery_tick()
            time.sleep(0.01)

    def until(self, predicate, message, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.obj._output_recovery_tick()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(message)

    def retarget(self, identity):
        self.identities["Studio Monitor"] = identity
        changed = self.obj._reload_output_recovery_identity()
        if changed != {"studio_monitor"}:
            raise AssertionError(f"identity reload mismatch: {changed}")
        if not self.obj._apply_audio_output_device(
                self.monitor.legacy_device, identity_changed=True):
            raise AssertionError(f"retarget {identity} rejected")

    def healthy(self, identity, timeout=8):
        expected = f"plughw:CARD={identity},DEV=0"
        self.until(
            lambda: (
                self.monitor.coordinator.state == audio_recovery.SlotState.OK
                and self.factory.current(self.monitor) is not None
                and self.factory.current(self.monitor)["device"] == expected
                and not self.monitor.valve.get_property("drop")
                and not self.monitor.retarget_in_progress),
            f"{identity} did not become healthy", timeout)

    def failed(self, identity):
        self.until(
            lambda: (
                self.monitor.coordinator.state == audio_recovery.SlotState.DEGRADED
                and self.monitor.current_bin is None
                and self.monitor.pending_bin is None
                and self.monitor.coordinator.snapshot()["operation_state"] != "IN_FLIGHT"
                and self.monitor.valve.get_property("drop")),
            f"{identity} failure did not settle")

    def recover(self, identity):
        self.monitor.next_retry_at = None
        with patch.object(audio_recovery, "read_alsa_cards_present",
                          lambda: {identity: 7}):
            self.obj._output_presence_probe_tick()
        self.healthy(identity)

    @staticmethod
    def delta(before, after):
        samples = after["sample_count"] - before["sample_count"]
        abs_sum = after["sample_abs_sum"] - before["sample_abs_sum"]
        square_sum = after["sample_square_sum"] - before["sample_square_sum"]
        return {
            "buffers": after["buffer_count"] - before["buffer_count"],
            "nonzero_buffers": after["nonzero_buffers"] - before["nonzero_buffers"],
            "all_zero_buffers": after["all_zero_buffers"] - before["all_zero_buffers"],
            "sample_count": samples,
            "mean_abs": abs_sum / samples if samples else None,
            "rms": math.sqrt(max(0.0, square_sum) / samples) if samples else None,
        }

    def open_phase(self, name):
        current = self.factory.current(self.monitor)
        probes = {"P1": self.P1, "P2": self.P2, "P3": self.P3, "P4": current["P4"]}
        capture = self.captures.get(self.monitor.identity)
        if capture:
            probes["P5"] = capture.probe
        before = {key: probe.snapshot() for key, probe in probes.items()}
        sibling_before = self.sibling.snapshot()
        rendered_before = engine._output_sink_rendered_count(self.monitor.current_sink)
        self.pump(self.args.window_s)
        after = {key: probe.snapshot() for key, probe in probes.items()}
        sibling_after = self.sibling.snapshot()
        rendered_after = engine._output_sink_rendered_count(self.monitor.current_sink)
        deltas = {key: self.delta(before[key], after[key]) for key in probes}
        for key in ("P1", "P2", "P3", "P4"):
            if deltas[key]["nonzero_buffers"] <= 0:
                raise AssertionError(f"{name}: {key} lacks nonzero PCM: {deltas[key]}")
            if "format=(string)F32LE" not in (after[key]["caps"] or ""):
                raise AssertionError(f"{name}: {key} caps mismatch: {after[key]['caps']}")
        if "P5" in probes:
            p5 = deltas["P5"]
            if p5["nonzero_buffers"] <= 0:
                raise AssertionError(f"{name}: P5 lacks nonzero physical capture")
            # Exact nonzero is sufficient at the digital P1-P4 boundaries,
            # but an analog capture always contains some noise. Require
            # phase-local energy above an explicit floor so noise cannot be
            # mistaken for a successfully propagated program signal.
            if p5["rms"] is None or p5["rms"] < self.args.p5_min_rms:
                raise AssertionError(
                    f"{name}: P5 RMS {p5['rms']!r} is below physical-signal "
                    f"floor {self.args.p5_min_rms}")
        sibling_delta = self.delta(sibling_before, sibling_after)
        if sibling_delta["nonzero_buffers"] <= 0:
            raise AssertionError(f"{name}: StereoTool stopped or went silent")
        if rendered_after is not None and rendered_before is not None:
            if rendered_after <= rendered_before:
                raise AssertionError(f"{name}: sink rendered count did not advance")
        self.phases.append({
            "name": name, "kind": "open_signal_integrity",
            "identity": self.monitor.identity,
            "generation": self.monitor.coordinator.generation,
            "ownership_epoch": self.monitor.device_loss_epoch(),
            "valve_drop": self.monitor.valve.get_property("drop"),
            "probe_deltas": deltas, "probe_after": after,
            "sibling_delta": sibling_delta,
            "sink_rendered_before": rendered_before,
            "sink_rendered_after": rendered_after,
        })

    def failed_phase(self, name):
        probes = {"P1": self.P1, "P2": self.P2, "P3": self.P3}
        before = {key: probe.snapshot() for key, probe in probes.items()}
        sibling_before = self.sibling.snapshot()
        self.pump(self.args.window_s)
        after = {key: probe.snapshot() for key, probe in probes.items()}
        deltas = {key: self.delta(before[key], after[key]) for key in probes}
        sibling_delta = self.delta(sibling_before, self.sibling.snapshot())
        if deltas["P1"]["nonzero_buffers"] <= 0 or deltas["P2"]["nonzero_buffers"] <= 0:
            raise AssertionError(f"{name}: upstream Studio boundary went silent")
        if deltas["P3"]["buffers"] != 0:
            raise AssertionError(f"{name}: closed valve leaked P3 buffers")
        if sibling_delta["nonzero_buffers"] <= 0:
            raise AssertionError(f"{name}: StereoTool stopped or went silent")
        self.phases.append({
            "name": name, "kind": "failed_target_containment",
            "identity": self.monitor.identity,
            "generation": self.monitor.coordinator.generation,
            "ownership_epoch": self.monitor.device_loss_epoch(),
            "slot_state": self.monitor.coordinator.state.value,
            "operation_state": self.monitor.coordinator.snapshot()["operation_state"],
            "retarget_in_progress": self.monitor.retarget_in_progress,
            "valve_drop": self.monitor.valve.get_property("drop"),
            "probe_deltas": deltas, "sibling_delta": sibling_delta,
        })

    def sibling_invariant(self):
        sibling = self.sibling.snapshot()
        if sibling["max_inter_buffer_gap_s"] >= SIBLING_GAP_LIMIT_S:
            raise AssertionError(
                f"StereoTool gap {sibling['max_inter_buffer_gap_s']:.6f}s")
        bad = [
            value for _, value in self.telemetry.snapshot()
            if value.get("pipeline_state") != "playing"
            or value.get("stereotool_state") != "playing"
        ]
        if bad:
            raise AssertionError(f"pipeline/StereoTool left PLAYING: {bad[:3]}")
        names = {self.pipeline.get_name(), self.stereo.current_bin.get_name()}
        bad_bus = [
            msg for msg in self.bus.snapshot()
            if msg.get("ts", 0) >= self.started_at
            and msg.get("type") == "state-changed"
            and msg.get("src") in names
            and msg.get("new") != "playing"
        ]
        if bad_bus:
            raise AssertionError(f"pipeline/StereoTool transition: {bad_bus}")

    def _synth_loss(self):
        msg = fake_error_message(
            "gst-resource-error-quark: Error outputting to audio device. "
            "The device has been disconnected. (10)")
        self.obj._on_output_error(self.monitor, None, msg)

    def report(self):
        generations = []
        for meta in sorted(self.factory.generations.values(),
                           key=lambda value: value["serial"]):
            generations.append({
                "serial": meta["serial"], "device": meta["device"],
                "failed": meta["failed"], "P4": meta["P4"].snapshot(),
                "sink_input": meta["sink_input"].snapshot(),
            })
        return {
            "mode": (
                "physical_P5_" + "_".join(sorted(self.captures))
                if self.args.physical else "synthetic_safe"
            ),
            "claim": (
                "Generation-local PCM propagation and sibling continuity; "
                "not reproduction or root-cause proof of the production-zero incident."
            ),
            "phases": self.phases,
            "P1": self.P1.snapshot(), "P2": self.P2.snapshot(),
            "P3": self.P3.snapshot(), "StereoTool": self.sibling.snapshot(),
            "P5": (
                {name: cap.probe.snapshot() for name, cap in self.captures.items()}
                if self.captures else
                {"available": False, "reason": "safe synthetic mode"}
            ),
            "generations": generations,
            "generation_calls": self.factory.calls,
            "queue_signals": self.queue_signals.snapshot(),
            "queue_leaky": self.monitor.queue.get_property("leaky").value_nick,
            "telemetry": self.telemetry.snapshot(),
            "bus_messages": self.bus.snapshot(),
        }


def execute(args):
    harness = Harness(args)
    old_emit = engine.emit_event
    engine.emit_event = lambda *a, **kw: None
    report = None
    try:
        harness.start()
        harness.open_phase(f"cold_start_{harness.monitor.identity}")
        if args.stop_after_cold:
            harness.sibling_invariant()
            report = harness.report()
            report["result"] = "PASS"
            return report
        if harness.monitor.identity != "PCH":
            raise AssertionError(
                "full scenario sequence requires --initial-identity PCH")
        for identity, name in (
                ("CODEC", "successful_PCH_to_CODEC"),
                ("PCH", "successful_CODEC_to_PCH")):
            harness.retarget(identity)
            harness.healthy(identity)
            harness.open_phase(name)

        for failure_mode in ("missing", "busy"):
            identity = "CODEC"
            runtime = "plughw:CARD=CODEC,DEV=0"
            harness.factory.fail.add(runtime)
            harness.retarget(identity)
            harness.failed(identity)
            harness.failed_phase(f"{failure_mode}_CODEC_target_degraded")
            harness.factory.fail.remove(runtime)
            harness.recover(identity)
            harness.open_phase(f"{failure_mode}_CODEC_target_recovered")
            harness.retarget("PCH")
            harness.healthy("PCH")
            harness.open_phase(f"return_from_{failure_mode}_CODEC_to_PCH")

        for index, identity in enumerate(
                ("CODEC", "PCH", "CODEC", "PCH", "CODEC", "PCH"), 1):
            harness.retarget(identity)
            harness.healthy(identity)
            harness.open_phase(f"repeat_{index}_{identity}")

        epoch = harness.monitor.device_loss_epoch()
        harness._synth_loss()
        harness.until(
            lambda: (
                harness.monitor.coordinator.state == audio_recovery.SlotState.DEGRADED
                and harness.monitor.current_bin is None),
            "device-loss quiesce did not settle")
        with patch.object(engine, "OUTPUT_HEALTH_STABILIZATION_S", 0.5):
            harness.obj._output_dispatch_rebuild(harness.monitor)
            harness.until(
                lambda: (
                    harness.monitor.pending_bin is not None
                    and harness.monitor.coordinator.state
                    == audio_recovery.SlotState.RECOVERING),
                "race candidate was not pending")
            stale = harness.monitor.pending_bin
            harness.retarget("CODEC")
            harness.healthy("CODEC", 10)
        if harness.monitor.current_bin is stale:
            raise AssertionError("stale race candidate promoted")
        if harness.monitor.device_loss_epoch() < epoch + 2:
            raise AssertionError("loss + retarget did not advance epoch twice")
        harness.open_phase("retarget_device_loss_race_recovered_CODEC")
        harness.sibling_invariant()
        report = harness.report()
        report["result"] = "PASS"
        return report
    except Exception as exc:
        report = harness.report()
        report["result"] = "FAIL"
        report["failure"] = repr(exc)
        raise
    finally:
        if report is not None:
            dump_json(args.output, report)
        harness.stop()
        engine.emit_event = old_emit


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=f"/tmp/studio-boundary-harness-{int(time.time())}.json")
    parser.add_argument("--physical", action="store_true")
    parser.add_argument("--sink-a")
    parser.add_argument("--capture-a")
    parser.add_argument("--hw-status-a")
    parser.add_argument("--sink-b")
    parser.add_argument("--capture-b")
    parser.add_argument("--hw-status-b")
    parser.add_argument(
        "--p5-min-rms", type=float, default=0.005,
        help="minimum phase-local F32LE RMS accepted as physical P5 signal")
    parser.add_argument(
        "--window-s", type=float, default=0.30,
        help="settled measurement window for each scenario (default: 0.30)")
    parser.add_argument(
        "--physical-sink-sync-true", action="store_true",
        help="diagnostic only: restore the historical silent sync=True behavior")
    parser.add_argument(
        "--initial-identity", choices=("PCH", "CODEC"), default="PCH")
    parser.add_argument(
        "--stop-after-cold", action="store_true",
        help="run only the initial stable-identity generation")
    args = parser.parse_args()
    physical_sink_args = (
        args.sink_a, args.hw_status_a, args.sink_b, args.hw_status_b)
    any_physical_arg = any(physical_sink_args + (args.capture_a, args.capture_b))
    if args.physical and (
            not all(physical_sink_args)
            or not (args.capture_a or args.capture_b)):
        parser.error(
            "--physical requires both sink/hw-status pairs and at least one capture")
    if not args.physical and any_physical_arg:
        parser.error("ALSA arguments require explicit --physical")
    return args


def main():
    args = arguments()
    try:
        report = execute(args)
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "error": repr(exc),
                          "report": args.output}, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps({
        "result": report["result"], "mode": report["mode"],
        "report": args.output, "phases": len(report["phases"]),
        "stereotool_max_gap_s":
            report["StereoTool"]["max_inter_buffer_gap_s"],
        "stereotool_nonzero_buffers":
            report["StereoTool"]["nonzero_buffers"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
