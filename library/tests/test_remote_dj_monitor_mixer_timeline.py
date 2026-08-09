"""Isolated GStreamer regression harness for the Remote DJ monitor-return
silence bug (root-caused live 2026-08): a per-session `audiomixer`
dynamically created and inserted into the already-PLAYING main pipeline
(see library/services/engine.py's `_remote_dj_build_session`, `mon_mixer`)
inherited GstAggregator's `start-time-selection=zero` default. That makes
the mixer declare its own output segment starting at running-time 0
regardless of how long the parent pipeline has already been running, but
its real inputs arrive timestamped in the pipeline's CURRENT running-time
domain -- so the aggregator has to generate silence gap-fill to advance
its own zero-rooted output timeline up to the real, already-elapsed
running time before any real audio reaches WebRTC. The catch-up window
grows with engine (parent pipeline) uptime, matching the live symptom
exactly (WebRTC connects and RTP flows immediately, but every packet
carries silence for a stretch that got longer the longer the engine had
been up).

This is deliberately NOT a Django TestCase and does not import
library.services.engine or touch the database -- it exercises the real,
underlying GstAggregator behavior directly (real Gst.Pipeline, real
audiomixer, real state changes, a real running clock), the same
`start-time-selection` property engine.py's mon_mixer now sets, rather
than mocking the property assignment. `manage.py test` still discovers
and runs it via normal unittest discovery.

No literal multi-hour wait is needed (or used) to reproduce the
architectural condition -- a short, deterministic "head start" (the
parent pipeline is PLAYING and its running time has materially advanced
BEFORE the dynamic mixer is even created) is enough to prove the
misalignment directly via the mixer's own first output buffer metadata
(its PTS, and whether GstAggregator marked it a synthesized GAP/silence
buffer), without waiting for a real, proportionally-long catch-up to
fully drain."""
import struct
import time
import unittest

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstBase", "1.0")
from gi.repository import Gst, GstBase  # noqa: E402

Gst.init(None)

# How long the parent pipeline is left PLAYING (its running time
# advancing) before the dynamically-created monitor mixer is built --
# "a few seconds" per the task, deliberately short so the whole test
# module runs quickly; the misalignment this reproduces scales with
# this value (in production, with hours of engine uptime) but is
# already fully measurable at this scale.
HEAD_START_S = 1.5

# How long to wait for the mixer's first output buffer before treating
# the run as a hung/broken test (never expected to trigger in a working
# environment -- GstAggregator emits its first buffer, real or
# gap-filled, within tens of milliseconds of reaching PLAYING).
PROBE_TIMEOUT_S = 10.0

# Loose but meaningful tolerances -- this harness cares about "did the
# mixer's own timeline start near 0 (broken) or near the real current
# running time (fixed)", not sub-millisecond precision.
NEAR_ZERO_TOLERANCE_S = 0.5
NEAR_CURRENT_TOLERANCE_S = 0.5


def _build_and_measure_first_output_buffer(start_time_selection):
    """Builds a real parent Gst.Pipeline, sets it PLAYING, lets its
    running time advance by HEAD_START_S, THEN dynamically creates an
    audiomixer (with `start_time_selection` applied, mirroring
    engine.py's mon_mixer.set_property call) fed by a real, non-silent
    live source (audiotestsrc, sine wave) -- i.e. exactly the
    architectural shape of the production bug: a dynamically-inserted
    aggregator whose real input buffers carry the PARENT pipeline's
    current running-time timestamps.

    Returns a dict: running_time_at_creation_ns (the parent pipeline's
    own running time at the moment the mixer was built -- what a
    correctly-aligned mixer's first output should be close to),
    first_pts_ns (the mixer's own first output buffer's PTS), is_gap
    (whether GstAggregator flagged that first buffer as synthesized
    silence), and wall_elapsed_s (real wall-clock time from mixer
    creation to that first buffer arriving, for reference)."""
    pipeline = Gst.Pipeline.new("mon_mixer_timeline_test")
    ret = pipeline.set_state(Gst.State.PLAYING)
    assert ret != Gst.StateChangeReturn.FAILURE, "parent pipeline failed to reach PLAYING"
    pipeline.get_state(Gst.CLOCK_TIME_NONE)

    time.sleep(HEAD_START_S)

    clock = pipeline.get_clock()
    assert clock is not None, "parent pipeline has no clock once PLAYING"
    running_time_at_creation_ns = clock.get_time() - pipeline.get_base_time()

    # Same topology shape as engine.py's monitor-return branch: a live
    # source feeding a queue feeding the dynamically-configured mixer,
    # added to the ALREADY-PLAYING parent pipeline (not built alongside
    # it) -- this is what makes the new elements inherit the parent's
    # already-established base_time/clock, and therefore what makes
    # audiotestsrc's own first buffer arrive stamped at the CURRENT
    # running time rather than at 0, faithfully reproducing the real
    # remote_dj_tee/local_mic_tee input shape without needing an actual
    # tee.
    src = Gst.ElementFactory.make("audiotestsrc", None)
    assert src is not None, "audiotestsrc element not available in this GStreamer install"
    src.set_property("is-live", True)
    src.set_property("wave", 0)  # sine -- genuinely non-silent
    src.set_property("freq", 440.0)
    q = Gst.ElementFactory.make("queue", None)
    mixer = Gst.ElementFactory.make("audiomixer", None)
    assert mixer is not None, "audiomixer element not available in this GStreamer install"
    mixer.set_property("start-time-selection", start_time_selection)
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", False)  # observe buffers as soon as produced, not clock-paced by the sink

    for el in (src, q, mixer, sink):
        pipeline.add(el)
    assert src.link(q)
    assert q.link(mixer)
    assert mixer.link(sink)

    result = {}
    mon_start_wall = time.time()

    def on_buffer(pad, info, _udata):
        if "first_pts_ns" not in result:
            buf = info.get_buffer()
            result["first_pts_ns"] = buf.pts
            result["is_gap"] = bool(buf.get_flags() & Gst.BufferFlags.GAP)
            result["wall_elapsed_s"] = time.time() - mon_start_wall
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if ok:
                try:
                    n = mapinfo.size // 2
                    samples = struct.unpack(f"<{n}h", bytes(mapinfo.data)[: n * 2])
                    result["energy"] = sum(s * s for s in samples) / max(n, 1)
                finally:
                    buf.unmap(mapinfo)
        return Gst.PadProbeReturn.OK

    mixer.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer, None)

    for el in (src, q, mixer, sink):
        el.sync_state_with_parent()

    deadline = time.time() + PROBE_TIMEOUT_S
    while "first_pts_ns" not in result and time.time() < deadline:
        time.sleep(0.01)

    pipeline.set_state(Gst.State.NULL)
    pipeline.get_state(Gst.CLOCK_TIME_NONE)

    if "first_pts_ns" not in result:
        raise AssertionError(
            f"mixer produced no output buffer within {PROBE_TIMEOUT_S}s -- "
            "harness itself is broken (unrelated to the fix under test)"
        )

    return {
        "running_time_at_creation_ns": running_time_at_creation_ns,
        "first_pts_ns": result["first_pts_ns"],
        "is_gap": result["is_gap"],
        "wall_elapsed_s": result["wall_elapsed_s"],
        "energy": result.get("energy", 0.0),
    }


class OldZeroStartTimeSelectionReproducesSilenceBugTests(unittest.TestCase):
    """Demonstrates the PRE-FIX behavior in a controlled, fast way --
    the inherited GstAggregator default (`zero`) used before this round's
    fix. Confirms the harness itself is sound (it can reproduce the bug)
    before trusting it to validate the fix below. Per the task: does not
    wait out the full proportional catch-up delay -- asserts the
    timestamp/content misalignment directly on the mixer's own first
    output buffer."""

    def test_first_output_buffer_pts_starts_near_zero_not_current_running_time(self):
        m = _build_and_measure_first_output_buffer(GstBase.AggregatorStartTimeSelection.ZERO)
        first_pts_s = m["first_pts_ns"] / Gst.SECOND
        running_time_at_creation_s = m["running_time_at_creation_ns"] / Gst.SECOND

        # The parent pipeline had already been running for HEAD_START_S
        # (~1.5s) before the mixer was even created -- a correctly
        # time-aligned mixer's first output could never be this close
        # to zero.
        self.assertLess(
            first_pts_s, NEAR_ZERO_TOLERANCE_S,
            f"expected ZERO start-time-selection's first output PTS to start near 0s "
            f"(got {first_pts_s:.3f}s, with the parent pipeline already {running_time_at_creation_s:.3f}s "
            f"into its running time when the mixer was created) -- this is the misalignment "
            f"the fix exists to eliminate",
        )

    def test_first_output_buffer_is_synthesized_silence(self):
        # GstAggregator marks its own generated gap-fill with the GAP
        # buffer flag -- this is the direct GStreamer-level analog of
        # the live symptom (Firefox getStats(): totalAudioEnergy=0
        # despite tens of thousands of packetsReceived, each a fixed
        # 3-byte Opus silence frame).
        m = _build_and_measure_first_output_buffer(GstBase.AggregatorStartTimeSelection.ZERO)
        self.assertTrue(
            m["is_gap"],
            "expected ZERO start-time-selection's first output buffer to be GstAggregator-"
            "synthesized silence (GAP flag set) -- real audio isn't available yet at "
            "running-time 0, only far in this mixer's own zero-rooted future",
        )
        self.assertEqual(
            m["energy"], 0.0,
            "expected zero sample energy in ZERO start-time-selection's first output buffer",
        )


class FixedFirstStartTimeSelectionEliminatesSilenceBugTests(unittest.TestCase):
    """Demonstrates the FIXED behavior -- start-time-selection=FIRST,
    exactly what _remote_dj_build_session's mon_mixer now sets. This
    test must fail if that property is ever reverted to the inherited
    ZERO default (or removed): swap the argument below to ZERO and it
    reduces to (and fails the same way as) the "old" test class above."""

    def test_first_output_buffer_pts_starts_near_current_running_time(self):
        m = _build_and_measure_first_output_buffer(GstBase.AggregatorStartTimeSelection.FIRST)
        first_pts_s = m["first_pts_ns"] / Gst.SECOND
        running_time_at_creation_s = m["running_time_at_creation_ns"] / Gst.SECOND

        delta_s = abs(running_time_at_creation_s - first_pts_s)
        self.assertLess(
            delta_s, NEAR_CURRENT_TOLERANCE_S,
            f"expected FIRST start-time-selection's first output PTS ({first_pts_s:.3f}s) to "
            f"land close to the parent pipeline's running time when the mixer was created "
            f"({running_time_at_creation_s:.3f}s) -- got a {delta_s:.3f}s misalignment, which "
            f"is exactly the bug this property exists to prevent",
        )

    def test_first_output_buffer_is_real_non_silent_audio(self):
        m = _build_and_measure_first_output_buffer(GstBase.AggregatorStartTimeSelection.FIRST)
        self.assertFalse(
            m["is_gap"],
            "expected FIRST start-time-selection's first output buffer to be real "
            "(non-GAP) audio, not GstAggregator-synthesized silence",
        )
        self.assertGreater(
            m["energy"], 0.0,
            "expected nonzero sample energy in FIRST start-time-selection's first output "
            "buffer -- the sine-wave source should already be audible immediately, with no "
            "zero-timeline catch-up window",
        )

    def test_no_long_catch_up_wall_delay(self):
        # With FIRST, the mixer's own segment starts exactly where the
        # real data already is -- so its first output buffer should
        # appear almost immediately in wall-clock terms too, not after
        # a delay proportional to HEAD_START_S (which is what a
        # zero-rooted catch-up would need to drain first).
        m = _build_and_measure_first_output_buffer(GstBase.AggregatorStartTimeSelection.FIRST)
        self.assertLess(
            m["wall_elapsed_s"], NEAR_CURRENT_TOLERANCE_S,
            f"expected the first output buffer within a fraction of a second of mixer "
            f"creation, got {m['wall_elapsed_s']:.3f}s -- suggests a catch-up delay is "
            f"still happening",
        )
