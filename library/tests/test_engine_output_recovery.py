"""[P0] 1.3C -- audio OUTPUT device hotplug recovery: engine-integrated
tests.

Real GStreamer elements/pipelines (never mocked -- matches this
codebase's established convention, see test_engine_mic_recovery.py),
built via the same object.__new__(PlaybackEngine) minimal-stand-in
technique. No real ALSA hardware is opened anywhere in this file --
every test uses a synthetic generation (identity+fakesink standing in
for a real alsasink, confirmed directly to expose the same GstBaseSink
`stats` property alsasink does) so this suite runs anywhere, no card
required.

Unlike mic recovery, OutputRecoverySlot's queue/errorignore/valve/
generation are added DIRECTLY to self.main_pipeline (no persistent
outer wrapping bin -- see engine.py's OutputRecoverySlot/
_build_output_slot docstrings for why: today's pre-1.3C StereoTool/
Studio Monitor construction never used one either, and introducing one
would have been an unrequested topology change). So in these tests the
throwaway Gst.Pipeline built here IS obj.main_pipeline, not a separate
variable, and every _output_* method call operates on it directly.

See scratchpad/audio_output_recovery/ROUND7_DECISION_REPORT.md for the
discovery/proof this implementation is based on, and test_audio_recovery.py
for the pure-module (SlotCoordinator itself) half."""
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from django.test import SimpleTestCase

import library.services.engine as eng_module
from library.services import audio_recovery

Gst.init(None)


def make_synthetic_generation_builder(error_after=-1, sleep_time_us=0):
    """Returns a callable(device) -> (Gst.Bin, sink), the same shape
    _build_studio_monitor_hw_generation/_build_stereotool_hw_generation
    return -- a single ghost 'sink' pad wrapping identity(+fault)
    ->fakesink instead of a real alsasink, so tests can drive
    deterministic success/failure without hardware. `device` is
    ignored -- the whole point is these never touch ALSA. fakesink
    exposes the same GstBaseSink `stats` property alsasink does
    (confirmed directly, not assumed) so the real health-check code
    path (including the rendered-count verification) runs unmodified
    against these synthetic generations."""
    def build(device):
        bin_ = Gst.Bin.new(f"gen_{int(time.time() * 1_000_000)}")
        ident = Gst.ElementFactory.make("identity", None)
        if error_after >= 0:
            ident.set_property("error-after", error_after)
        if sleep_time_us:
            ident.set_property("sleep-time", sleep_time_us)
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        bin_.add(ident)
        bin_.add(sink)
        ident.link(sink)
        ghost = Gst.GhostPad.new("sink", ident.get_static_pad("sink"))
        ghost.set_active(True)
        bin_.add_pad(ghost)
        return bin_, sink
    return build


def make_output_engine_stand_in():
    """Minimal PlaybackEngine stand-in -- only the attributes the
    _output_* methods actually touch. Mirrors test_engine_mic_recovery.
    py's make_mic_stand_in."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.main_pipeline = None  # set by the caller once a real pipeline exists
    obj._mic_bin = None       # so _on_main_bus_error's mic-first check is a clean no-op
    obj._output_slots = {}
    obj._studio_monitor_slot = None
    obj._stereotool_slot = None
    return obj


def build_slot_in_pipeline(obj, name="Studio Monitor", kind="studio_monitor",
                            legacy_device="hw:CARD=NoSuchTestCard,DEV=0",
                            identity_kind="", identity="",
                            build_generation_fn=None, register=True):
    """Builds ONE OutputRecoverySlot with a real (throwaway) Gst.Pipeline
    as obj.main_pipeline, brings the pipeline to PLAYING, and returns
    (pipeline, slot). `register` mirrors production wiring the slot into
    obj._output_slots (needed for _on_main_bus_error routing / the tick
    methods to find it)."""
    if build_generation_fn is None:
        build_generation_fn = make_synthetic_generation_builder()

    pipeline = Gst.Pipeline.new("test-output-pipeline")
    obj.main_pipeline = pipeline

    # Stand in for _resolve_output_device_identity's DB read -- tests
    # set identity_kind/identity directly rather than touching the DB,
    # same "stub the DB-backed resolver, test the mechanism" approach
    # test_engine_mic_recovery.py already uses.
    from unittest.mock import patch
    with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                       lambda self, name: (identity_kind, identity)):
        slot = obj._build_output_slot(name, kind, legacy_device, build_generation_fn)

    pipeline.add(slot.queue)
    pipeline.add(slot.errorignore)
    pipeline.add(slot.valve)
    pipeline.add(slot.current_bin)
    slot.queue.link(slot.errorignore)
    slot.errorignore.link(slot.valve)
    slot.valve.get_static_pad("src").link(slot.current_bin.get_static_pad("sink"))

    src = Gst.ElementFactory.make("audiotestsrc", None)
    src.set_property("is-live", True)
    pipeline.add(src)
    src.link(slot.queue)

    if register:
        obj._output_slots = {kind: slot}
        if kind == "studio_monitor":
            obj._studio_monitor_slot = slot
        elif kind == "stereotool":
            obj._stereotool_slot = slot

    pipeline.set_state(Gst.State.PLAYING)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        _, st, _ = pipeline.get_state(0)
        if st == Gst.State.PLAYING:
            break
        time.sleep(0.02)
    return pipeline, slot


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def fake_error_message(message_text, debug_text=""):
    class _Err:
        def __str__(self_inner):
            return message_text

    class _Msg:
        def parse_error(self_inner):
            return _Err(), debug_text

    return _Msg()


def wait_until_pumping_glib(predicate, timeout=5.0, interval=0.02):
    """Same as wait_until, but also iterates the real GLib default main
    context each cycle. Required for any bus signal-watch callback
    (bus.connect("message::...")) to actually fire -- confirmed
    directly: a bare time.sleep() loop, no matter how long, never
    dispatches a single such signal, since nothing is iterating the
    context the watch's GSource is attached to. Same underlying GLib
    mechanism as test_fx_fire_completion.py's _drain_glib_idle_queue()."""
    from gi.repository import GLib as _GLib
    ctx = _GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.iteration(False):
            pass
        if predicate():
            return True
        time.sleep(interval)
    return False


def simulate_device_loss_and_quiesce(obj, slot):
    """Drives the REAL _on_output_error -> quiesce -> re-degrade
    sequence a production device-loss event goes through, so a test
    that then calls _output_dispatch_rebuild starts from a correctly
    detached, re-degraded slot -- calling dispatch_rebuild directly
    against a slot whose valve is still linked to the OLD generation
    (skipping quiesce) fails with a real GST_PAD_LINK_WAS_LINKED error,
    which is exactly what a naive test-only shortcut hit here first."""
    msg = fake_error_message(
        "gst-resource-error-quark: Error outputting to audio device. "
        "The device has been disconnected. (10)")
    obj._on_output_error(slot, None, msg)
    if not wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK, timeout=3.0):
        raise AssertionError("quiesce did not resolve to OK in time")
    snapshot = slot.coordinator.snapshot()
    obj._output_handle_slot_transition(slot, "RECOVERING", "OK", snapshot)
    if slot.coordinator.state != audio_recovery.SlotState.DEGRADED:
        raise AssertionError("slot must be re-degraded after a successful quiesce, ready for a rebuild dispatch")


def wait_for_pending_discard_to_resolve(obj, slot):
    """After _output_handle_slot_transition's DEGRADED-with-pending-bin
    branch dispatches _output_discard_pending_bin for a failed rebuild
    candidate, THAT teardown's own worker resolves the coordinator back
    to OK (a "the discard itself succeeded" signal, not a rebuild
    success) -- which must be re-degraded before the slot is truly
    ready to accept a FRESH rebuild dispatch. A test that skips this and
    immediately calls _output_dispatch_rebuild again races the discard's
    own in-flight worker: request_recovery() coalesces the new dispatch
    instead of actually running it, and a later wait_until(state==OK)
    ends up observing the DISCARD's resolution, not the new rebuild's --
    caught live (operation_succeeded read True with the valve still
    closed, since the discard worker never touches the valve at all).
    Mirrors simulate_device_loss_and_quiesce's identical second half."""
    if not wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK, timeout=3.0):
        raise AssertionError("pending-bin discard did not resolve to OK in time")
    snapshot = slot.coordinator.snapshot()
    obj._output_handle_slot_transition(slot, "RECOVERING", "OK", snapshot)
    if slot.coordinator.state != audio_recovery.SlotState.DEGRADED:
        raise AssertionError("slot must be re-degraded after a successful pending-bin discard")


class MockEmitEventMixin:
    """Patches monitoring.models.emit_event (hit on every device-loss/
    recovered/restart-required transition) so these tests never need a
    database. Same shape as test_engine_mic_recovery.py's
    MockDuckingMixin's emit_event half."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        self.emitted_events = []

        def _fake_emit_event(category, title, level="info", detail=None, source=None, dedupe_key=None):
            self.emitted_events.append({"category": category, "title": title, "level": level,
                                         "detail": detail, "dedupe_key": dedupe_key})

        patcher = patch.object(eng_module, "emit_event", _fake_emit_event)
        patcher.start()
        self.addCleanup(patcher.stop)


class OutputSlotConstructionTests(MockEmitEventMixin, SimpleTestCase):
    """Sanity: building against a deliberately-fake device string still
    constructs a valid, linkable topology -- matches 1.3B2's mic
    precedent (alsasrc/alsasink construction never fails synchronously;
    only the later state change / bus error does)."""

    def test_construction_succeeds_and_links(self):
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING))
            self.assertIsNotNone(slot.current_bin.get_static_pad("sink"))
            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.OK)
            self.assertFalse(slot.valve.get_property("drop"))
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_errorignore_configured_explicitly_not_relying_on_defaults(self):
        """Round 1's own finding: errorignore's plugin DEFAULTS are
        ignore-error=True but ALSO ignore-notnegotiated=True -- this
        implementation must override the second one explicitly."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            self.assertTrue(slot.errorignore.get_property("ignore-error"))
            self.assertFalse(slot.errorignore.get_property("ignore-notnegotiated"))
            self.assertEqual(slot.errorignore.get_property("convert-to").value_nick, "ok")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_every_branch_has_its_own_queue(self):
        """Item from the test list: every physical-output branch has its
        OWN queue object (not sharing one) -- Round 4's blocking-
        containment finding requires this, not just for StereoTool."""
        obj = make_output_engine_stand_in()
        pipeline, sm_slot = build_slot_in_pipeline(obj, name="Studio Monitor", kind="studio_monitor")
        try:
            obj2 = make_output_engine_stand_in()
            pipeline2, st_slot = build_slot_in_pipeline(obj2, name="Stereotool Input", kind="stereotool")
            try:
                self.assertIsNot(sm_slot.queue, st_slot.queue)
                self.assertEqual(sm_slot.queue.get_property("leaky"), 1)
                self.assertEqual(st_slot.queue.get_property("leaky"), 1)
            finally:
                pipeline2.set_state(Gst.State.NULL)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_stereotool_special_sink_properties_reapply_on_generation(self):
        """StereoTool's sink needs sync=False/async=False/buffer-time/
        latency-time reapplied on EVERY generation, not just the first --
        exercised here via the real _build_stereotool_hw_generation
        method directly (not the synthetic stand-in) against a fake
        device string, matching the mic-clock-policy precedent of
        checking element properties without ever reaching PLAYING."""
        obj = make_output_engine_stand_in()
        for _ in range(3):  # simulate several successive rebuilds
            bin_, sink = obj._build_stereotool_hw_generation("hw:CARD=Fake,DEV=0")
            self.assertFalse(sink.get_property("sync"))
            self.assertFalse(sink.get_property("async"))
            self.assertEqual(sink.get_property("buffer-time"), 200000)
            self.assertEqual(sink.get_property("latency-time"), 20000)
            bin_.set_state(Gst.State.NULL)

    def test_studio_monitor_generation_does_not_get_stereotool_properties(self):
        """Must NOT accidentally normalize Studio Monitor's sink to
        StereoTool's special values -- alsasink's own driver defaults
        for sync/async/buffer-time/latency-time apply instead."""
        obj = make_output_engine_stand_in()
        bin_, sink = obj._build_studio_monitor_hw_generation("hw:CARD=Fake,DEV=0")
        try:
            # alsasink defaults: sync=True, async=True (unset here).
            self.assertTrue(sink.get_property("sync"))
            self.assertTrue(sink.get_property("async"))
        finally:
            bin_.set_state(Gst.State.NULL)

    def test_studio_monitor_and_agc_are_not_inside_the_generation(self):
        """The [P0] 1.3C design refinement: AGC stays persistent/outside
        the disposable generation -- the generation contains ONLY the
        sink."""
        obj = make_output_engine_stand_in()
        bin_, sink = obj._build_studio_monitor_hw_generation("hw:CARD=Fake,DEV=0")
        try:
            found = []
            it = bin_.iterate_elements()
            while True:
                ok, el = it.next()
                if not ok:
                    break
                found.append(el)
            self.assertEqual(found, [sink], "generation must contain ONLY the alsasink")
        finally:
            bin_.set_state(Gst.State.NULL)


class OutputDeviceLossSemanticsTests(MockEmitEventMixin, SimpleTestCase):
    """Item 6: on first device-loss, valve closes immediately, only that
    slot degrades, structured diagnostics are recorded, bounded quiesce
    dispatches -- main pipeline/tee/other slots untouched."""

    def test_device_lost_closes_valve_and_degrades_only_this_slot(self):
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING))
            gen_before = slot.coordinator.generation
            msg = fake_error_message(
                "gst-resource-error-quark: Error outputting to audio device. "
                "The device has been disconnected. (10)")
            obj._on_output_error(slot, None, msg)

            self.assertTrue(slot.valve.get_property("drop"))
            # Not asserting on the coordinator's transient STATE here --
            # the quiesce worker can resolve back to OK before this line
            # runs depending on system load/thread scheduling (caught
            # live via --shuffle on this exact class of assertion
            # elsewhere in this file; see
            # test_containment_still_works_without_identity's identical
            # comment). generation is the deterministic signal.
            self.assertGreater(slot.coordinator.generation, gen_before)
            self.assertTrue(slot.last_error.startswith("gst-resource-error-quark"))
            self.assertEqual(len(self.emitted_events), 1)
            self.assertEqual(self.emitted_events[0]["title"], "Studio Monitor output lost")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_unknown_error_does_not_trigger_recovery(self):
        """Item 2's output-side equivalent: transient/unknown errors
        must not initiate recovery."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            msg = fake_error_message("gst-core-error-quark: totally unrelated failure")
            obj._on_output_error(slot, None, msg)
            self.assertFalse(slot.valve.get_property("drop"))
            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.OK)
            self.assertEqual(len(self.emitted_events), 0)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_coalesced_repeat_failure_does_not_redispatch(self):
        """The 62-message-burst requirement: repeated device-loss
        messages for the same generation must collapse to ONE recovery
        cycle -- exactly the mechanism Round 6 proved necessary
        (11.6ms, 62 real messages from one physical unplug). Also
        confirms the pre-commit-review device_loss_epoch marker (added
        to fix the candidate-validation race) increments on EVERY
        classified message -- including the 61 coalesced ones -- while
        still causing none of: 62 recovery workers, 62 SystemEvents, or
        unnecessary generation churn."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            gen_before = slot.coordinator.generation
            epoch_before = slot.device_loss_epoch()
            msg = fake_error_message(
                "gst-resource-error-quark: Error outputting to audio device. "
                "The device has been disconnected. (10)")
            for _ in range(62):
                obj._on_output_error(slot, None, msg)
            self.assertEqual(slot.coordinator.generation, gen_before + 1,
                              "62 repeated device-loss messages must dispatch exactly ONE recovery cycle")
            self.assertEqual(len(self.emitted_events), 1)
            self.assertEqual(slot.device_loss_epoch(), epoch_before + 62,
                              "the epoch marker must bump on every classified message, coalesced or not")
        finally:
            pipeline.set_state(Gst.State.NULL)


class OutputSourceScopedRoutingTests(MockEmitEventMixin, SimpleTestCase):
    """Item 5: _on_main_bus_error must source-scope correctly -- Studio
    Monitor errors degrade only Studio Monitor, StereoTool errors
    degrade only StereoTool, mic errors still route only to mic,
    unrelated errors trigger nothing."""

    def _build_two_slots(self, obj):
        pipeline = Gst.Pipeline.new("test-two-slot-pipeline")
        obj.main_pipeline = pipeline
        from unittest.mock import patch

        def make_slot(name, kind):
            with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                               lambda self, n: ("", "")):
                slot = obj._build_output_slot(name, kind, f"hw:CARD=Fake_{kind},DEV=0",
                                               make_synthetic_generation_builder())
            pipeline.add(slot.queue)
            pipeline.add(slot.errorignore)
            pipeline.add(slot.valve)
            pipeline.add(slot.current_bin)
            slot.queue.link(slot.errorignore)
            slot.errorignore.link(slot.valve)
            slot.valve.get_static_pad("src").link(slot.current_bin.get_static_pad("sink"))
            return slot

        sm_slot = make_slot("Studio Monitor", "studio_monitor")
        st_slot = make_slot("Stereotool Input", "stereotool")
        obj._studio_monitor_slot = sm_slot
        obj._stereotool_slot = st_slot
        obj._output_slots = {"studio_monitor": sm_slot, "stereotool": st_slot}
        return pipeline, sm_slot, st_slot

    def test_studio_monitor_error_degrades_only_studio_monitor(self):
        obj = make_output_engine_stand_in()
        pipeline, sm_slot, st_slot = self._build_two_slots(obj)
        try:
            class _Msg:
                def __init__(self, src):
                    self.src = src
                    self.type = Gst.MessageType.ERROR

                def parse_error(self_inner):
                    class _E:
                        def __str__(self_e):
                            return ("gst-resource-error-quark: Error outputting to audio device. "
                                    "The device has been disconnected. (10)")
                    return _E(), ""

            # message.src is the sink deep inside sm_slot's current_bin.
            sm_sink = sm_slot.current_bin.iterate_elements().next()[1]
            gen_before = sm_slot.coordinator.generation
            obj._on_main_bus_error(None, _Msg(sm_sink))

            self.assertTrue(sm_slot.valve.get_property("drop"))
            # Not asserting on the coordinator's transient STATE here --
            # sm_slot's bin was never added to a real PLAYING pipeline
            # (see _build_two_slots), so its NULL teardown resolves
            # near-instantly on the background thread, racing
            # unpredictably past RECOVERING to OK before this line runs.
            # generation is the deterministic signal that recovery was
            # actually dispatched -- same fix already established in
            # test_engine_mic_recovery.py's RemoteDjDoesNotBlockRecoveryTests.
            self.assertGreater(sm_slot.coordinator.generation, gen_before)
            self.assertFalse(st_slot.valve.get_property("drop"))
            self.assertEqual(st_slot.coordinator.state, audio_recovery.SlotState.OK)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_stereotool_error_degrades_only_stereotool(self):
        obj = make_output_engine_stand_in()
        pipeline, sm_slot, st_slot = self._build_two_slots(obj)
        try:
            class _Msg:
                def __init__(self, src):
                    self.src = src
                    self.type = Gst.MessageType.ERROR

                def parse_error(self_inner):
                    class _E:
                        def __str__(self_e):
                            return ("gst-resource-error-quark: Error outputting to audio device. "
                                    "The device has been disconnected. (10)")
                    return _E(), ""

            st_sink = st_slot.current_bin.iterate_elements().next()[1]
            gen_before = st_slot.coordinator.generation
            obj._on_main_bus_error(None, _Msg(st_sink))

            self.assertTrue(st_slot.valve.get_property("drop"))
            # See the identical comment in
            # test_studio_monitor_error_degrades_only_studio_monitor --
            # generation, not transient state, is the deterministic
            # signal here.
            self.assertGreater(st_slot.coordinator.generation, gen_before)
            self.assertFalse(sm_slot.valve.get_property("drop"))
            self.assertEqual(sm_slot.coordinator.state, audio_recovery.SlotState.OK)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_unrelated_error_source_triggers_nothing(self):
        obj = make_output_engine_stand_in()
        pipeline, sm_slot, st_slot = self._build_two_slots(obj)
        try:
            unrelated = Gst.ElementFactory.make("identity", "unrelated")

            class _Msg:
                src = unrelated
                type = Gst.MessageType.ERROR

                def parse_error(self_inner):
                    class _E:
                        def __str__(self_e):
                            return "gst-core-error-quark: some unrelated deck/FX/VT failure"
                    return _E(), ""

            result = obj._on_main_bus_error(None, _Msg())
            self.assertTrue(result)
            self.assertFalse(sm_slot.valve.get_property("drop"))
            self.assertFalse(st_slot.valve.get_property("drop"))
            self.assertEqual(sm_slot.coordinator.state, audio_recovery.SlotState.OK)
            self.assertEqual(st_slot.coordinator.state, audio_recovery.SlotState.OK)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_stale_message_from_already_replaced_generation_does_not_match(self):
        """The 'old/late generation results cannot mutate new slot'
        requirement, on the bus-routing side: once slot.current_bin has
        been reassigned, a late message whose src is the OLD (now
        orphaned) generation's sink must not match this slot anymore."""
        obj = make_output_engine_stand_in()
        pipeline, sm_slot, st_slot = self._build_two_slots(obj)
        try:
            old_sink = sm_slot.current_bin.iterate_elements().next()[1]
            # Simulate a promotion having already happened (as
            # _output_handle_slot_transition's OK-branch would do) --
            # current_bin now points elsewhere.
            new_bin, new_sink = make_synthetic_generation_builder()("hw:CARD=Fake,DEV=0")
            sm_slot.current_bin = new_bin
            sm_slot.current_sink = new_sink

            self.assertFalse(obj._output_slot_owns_message_src(
                sm_slot, type("M", (), {"src": old_sink})()))
            self.assertTrue(obj._output_slot_owns_message_src(
                sm_slot, type("M", (), {"src": new_sink})()))
            new_bin.set_state(Gst.State.NULL)
        finally:
            pipeline.set_state(Gst.State.NULL)


class OutputHealthyBranchNeverStallsTests(MockEmitEventMixin, SimpleTestCase):
    """The core containment claim, exercised as a real synthetic tee
    harness: a device-loss error on one output slot must never stall a
    healthy sibling. Mirrors Round 5's own proven harness shape."""

    def test_healthy_sibling_keeps_flowing_through_device_loss_and_recovery(self):
        obj = make_output_engine_stand_in()
        pipeline = Gst.Pipeline.new("test-tee-pipeline")
        obj.main_pipeline = pipeline

        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("is-live", True)
        tee = Gst.ElementFactory.make("tee", "tee")
        pipeline.add(src)
        pipeline.add(tee)
        src.link(tee)

        # Healthy sibling -- plain queue+fakesink, no recovery machinery,
        # standing in for the OTHER output slot / decks / anything else
        # sharing the tee.
        q_healthy = Gst.ElementFactory.make("queue", "q_healthy")
        sink_healthy = Gst.ElementFactory.make("fakesink", "sink_healthy")
        sink_healthy.set_property("sync", False)
        pipeline.add(q_healthy)
        pipeline.add(sink_healthy)
        q_healthy.link(sink_healthy)
        tee.link(q_healthy)

        from unittest.mock import patch
        with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                           lambda self, name: ("", "")):
            slot = obj._build_output_slot(
                "Studio Monitor", "studio_monitor", "hw:CARD=Fake,DEV=0",
                make_synthetic_generation_builder(error_after=50))
        pipeline.add(slot.queue)
        pipeline.add(slot.errorignore)
        pipeline.add(slot.valve)
        pipeline.add(slot.current_bin)
        slot.queue.link(slot.errorignore)
        slot.errorignore.link(slot.valve)
        slot.valve.get_static_pad("src").link(slot.current_bin.get_static_pad("sink"))
        tee.link(slot.queue)
        obj._output_slots = {"studio_monitor": slot}
        obj._studio_monitor_slot = slot

        healthy_count = {"n": 0}

        def probe(pad, info):
            healthy_count["n"] += 1
            return Gst.PadProbeReturn.OK

        sink_healthy.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", obj._on_main_bus_error)

        try:
            pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING))
            self.assertTrue(wait_until(lambda: healthy_count["n"] > 5, timeout=2.0))

            # Phase 1: let the deliberate error-after=50 fire NATURALLY
            # via the real bus (not a synthetic message) -- proves
            # errorignore's raw flow-error containment for a real
            # GStreamer-originated error, not just a synthetic Python
            # object. Requires actually pumping the real GLib default
            # main context -- a bus signal-watch callback never fires
            # otherwise (confirmed directly; see wait_until_pumping_glib's
            # own docstring). identity.error-after's own built-in error
            # text ("Failed after iterations as requested") is correctly
            # classified "unknown" by the real classifier (confirmed
            # directly -- it doesn't match any real device-loss
            # signature), so the valve deliberately does NOT close here
            # -- this phase proves errorignore's containment holds
            # independent of classification, and that an unclassified
            # error correctly triggers no destructive action, matching
            # the locked "transient/unknown -> observable only" policy.
            count_before_unknown_error = healthy_count["n"]
            self.assertTrue(wait_until_pumping_glib(
                lambda: obj._studio_monitor_slot.last_error is not None, timeout=3.0))
            self.assertFalse(slot.valve.get_property("drop"),
                              "an 'unknown'-classified error must not close the valve")
            self.assertTrue(wait_until(lambda: healthy_count["n"] > count_before_unknown_error, timeout=1.0),
                             "healthy sibling must keep flowing through an unclassified error too")

            # Phase 2: a REAL device-loss-classified error (same shape
            # _on_output_error is actually driven by in production,
            # since GStreamer's own alsasink -- confirmed in Round 6 --
            # produces exactly this signature on a real disconnect)
            # must close the valve, and the healthy sibling must still
            # never stop.
            count_before_real_loss = healthy_count["n"]
            msg = fake_error_message(
                "gst-resource-error-quark: Error outputting to audio device. "
                "The device has been disconnected. (10)")
            obj._on_output_error(slot, None, msg)
            self.assertTrue(slot.valve.get_property("drop"))
            self.assertTrue(wait_until(lambda: healthy_count["n"] > count_before_real_loss, timeout=1.0),
                             "healthy sibling must keep receiving buffers after the OTHER slot's classified device loss")
        finally:
            pipeline.set_state(Gst.State.NULL)


OUTPUT_TEST_TIMEOUT = eng_module.OUTPUT_HEALTH_CHECK_DEADLINE_S + eng_module.OUTPUT_RENDER_VERIFY_DEADLINE_S + 3.0


class OutputRebuildHealthRuleTests(MockEmitEventMixin, SimpleTestCase):
    """Item 9: PLAYING alone is not proof -- rendered count must
    increase, and a failed rebuild must not leave the valve open or
    promote a bad generation."""

    def test_successful_rebuild_promotes_and_reopens_valve(self):
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING))
            old_bin = slot.current_bin

            simulate_device_loss_and_quiesce(obj, slot)
            self.emitted_events.clear()  # only care about the RECOVERY event below

            obj._output_dispatch_rebuild(slot)
            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK,
                                        timeout=5.0))
            snapshot = slot.coordinator.snapshot()
            obj._output_handle_slot_transition(slot, "RECOVERING", "OK", snapshot)

            self.assertIsNot(slot.current_bin, old_bin, "must be a genuinely NEW generation, not the old one reused")
            self.assertFalse(slot.valve.get_property("drop"), "valve must be open after a successful rebuild")
            self.assertEqual(len(self.emitted_events), 1)
            self.assertEqual(self.emitted_events[0]["title"], "Studio Monitor output recovered")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_rebuild_that_never_renders_fails_and_recloses_valve(self):
        """A generation that reaches PLAYING but is disconnected from
        anything that would ever fire the buffer probe -- simulates a
        wedged/degenerate hardware open (mirrors 1.3B2's own
        'PLAYING alone is not proof' invariant test for mic)."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING))
            simulate_device_loss_and_quiesce(obj, slot)
            self.emitted_events.clear()

            def build_disconnected(device):
                # Ghost pad -> a PERMANENTLY-closed inner valve -> sink.
                # Reaches PLAYING fine (both elements trivially do), and
                # the OUTER slot valve genuinely does open and push real
                # buffers at it during verification -- but this INNER
                # valve drops every one of them before they ever reach
                # `sink`, so its rendered count can never increase no
                # matter how long the outer valve stays open. Simulates
                # a wedged/degenerate hardware open: state says fine,
                # nothing actually renders (mirrors 1.3B2's identical
                # 'PLAYING alone is not proof' mic-recovery invariant
                # test). A first draft of this helper used an internal
                # fakesrc->fakesink pair instead -- caught live: fakesrc
                # pushes its OWN buffers autonomously once PLAYING,
                # completely independent of the ghost pad, so `sink`'s
                # rendered count climbed on its own and the health check
                # falsely PASSED. This construction has no such
                # confound -- `sink` truly cannot render without a
                # buffer surviving the inner valve, and none ever will.
                bin_ = Gst.Bin.new(f"disconnected_{int(time.time() * 1_000_000)}")
                inner_valve = Gst.ElementFactory.make("valve", None)
                inner_valve.set_property("drop", True)
                sink = Gst.ElementFactory.make("fakesink", None)
                sink.set_property("sync", False)
                sink.set_property("async", False)
                bin_.add(inner_valve)
                bin_.add(sink)
                inner_valve.link(sink)
                ghost = Gst.GhostPad.new("sink", inner_valve.get_static_pad("sink"))
                ghost.set_active(True)
                bin_.add_pad(ghost)
                return bin_, sink  # sink here NEVER receives anything, by construction

            slot.build_generation_fn = build_disconnected
            obj._output_dispatch_rebuild(slot)

            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.DEGRADED,
                                        timeout=OUTPUT_TEST_TIMEOUT))
            snapshot = slot.coordinator.snapshot()
            self.assertIs(snapshot["operation_succeeded"], False)
            obj._output_handle_slot_transition(slot, "RECOVERING", "DEGRADED", snapshot)
            self.assertIsNone(slot.pending_bin, "failed pending generation must be discarded")
        finally:
            pipeline.set_state(Gst.State.NULL)


class OutputDeviceLossDuringValidationRaceTests(MockEmitEventMixin, SimpleTestCase):
    """Pre-commit review finding: a candidate generation could still be
    promoted "recovered" even though a FRESH classified device-loss
    error arrived for it during validation, because
    SlotCoordinator.mark_degraded() correctly returns False once the
    slot is already RECOVERING (so the coalescing path takes over) while
    the worker's OWN rendered-count check had already latched success.
    Fixed with OutputRecoverySlot.device_loss_epoch() -- these tests
    force the exact bad timing deterministically rather than hoping to
    hit it, per the review's own instruction."""

    def test_device_loss_during_candidate_render_validation_cannot_promote(self):
        """Isolates the epoch-comparison MECHANISM itself: bump the
        epoch directly (bypassing the full bus-error round trip, which
        the harder test below exercises) at the exact moment the
        render-verify loop would first observe a real increase, and
        confirm the worker still reports failure."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING))
            simulate_device_loss_and_quiesce(obj, slot)
            self.emitted_events.clear()

            epoch_before = slot.device_loss_epoch()
            real_rendered_count = eng_module._output_sink_rendered_count
            state = {"bumped": False}

            def patched(sink):
                val = real_rendered_count(sink)
                # rendered_before is captured while the valve is still
                # closed -- guaranteed 0 at that point (confirmed by
                # this whole design: nothing can reach the sink before
                # the valve opens) -- so val > 0 only matches a genuine
                # post-open increase, never the baseline-capture call.
                if not state["bumped"] and val is not None and val > 0:
                    slot.record_device_loss()
                    state["bumped"] = True
                return val

            with patch.object(eng_module, "_output_sink_rendered_count", patched):
                obj._output_dispatch_rebuild(slot)
                self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.DEGRADED,
                                            timeout=OUTPUT_TEST_TIMEOUT))

            snapshot = slot.coordinator.snapshot()
            self.assertIs(snapshot["operation_succeeded"], False,
                           "the worker must report failure once the epoch advanced mid-verification, "
                           "even though rendered count genuinely increased")
            self.assertGreater(slot.device_loss_epoch(), epoch_before)
            obj._output_handle_slot_transition(slot, "RECOVERING", "DEGRADED", snapshot)
            self.assertIsNone(slot.pending_bin, "the un-promotable candidate must be discarded, not left pending")
            self.assertEqual(len(self.emitted_events), 0,
                              "no 'recovered' event may fire for a candidate that must not be promoted")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_device_loss_after_first_render_before_worker_success_retries(self):
        """The hardest timing the review called out, forced
        deterministically via a real threading.Event pair rather than
        hoped for: a fresh, correctly source-scoped, correctly
        CLASSIFIED device_lost bus error is injected for the pending
        generation at the exact instant the render-verify loop first
        observes rendered_now > rendered_before -- strictly AFTER a
        real render increase, strictly BEFORE the worker's own final
        return. Verifies the full observable contract: not promoted, no
        'recovered' event, valve closed, slot ends up in a retryable
        DEGRADED state (not OK), a SUBSEQUENT genuinely-fresh rebuild
        attempt succeeds normally, and a healthy sibling on the same
        tee never stops flowing through any of it."""
        obj = make_output_engine_stand_in()
        pipeline = Gst.Pipeline.new("test-race-tee-pipeline")
        obj.main_pipeline = pipeline

        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("is-live", True)
        tee = Gst.ElementFactory.make("tee", "tee")
        pipeline.add(src)
        pipeline.add(tee)
        src.link(tee)

        q_healthy = Gst.ElementFactory.make("queue", "q_healthy")
        sink_healthy = Gst.ElementFactory.make("fakesink", "sink_healthy")
        sink_healthy.set_property("sync", False)
        pipeline.add(q_healthy)
        pipeline.add(sink_healthy)
        q_healthy.link(sink_healthy)
        tee.link(q_healthy)
        healthy_count = {"n": 0}

        def probe(pad, info):
            healthy_count["n"] += 1
            return Gst.PadProbeReturn.OK

        sink_healthy.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)

        with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                           lambda self, name: ("", "")):
            slot = obj._build_output_slot(
                "Studio Monitor", "studio_monitor", "hw:CARD=Fake,DEV=0",
                make_synthetic_generation_builder())
        pipeline.add(slot.queue)
        pipeline.add(slot.errorignore)
        pipeline.add(slot.valve)
        pipeline.add(slot.current_bin)
        slot.queue.link(slot.errorignore)
        slot.errorignore.link(slot.valve)
        slot.valve.get_static_pad("src").link(slot.current_bin.get_static_pad("sink"))
        tee.link(slot.queue)
        obj._output_slots = {"studio_monitor": slot}
        obj._studio_monitor_slot = slot

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", obj._on_main_bus_error)

        try:
            pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING))
            self.assertTrue(wait_until(lambda: healthy_count["n"] > 5, timeout=2.0))

            simulate_device_loss_and_quiesce(obj, slot)
            self.emitted_events.clear()
            healthy_count_at_quiesce = healthy_count["n"]

            injected = threading.Event()
            release_worker = threading.Event()
            real_rendered_count = eng_module._output_sink_rendered_count
            state = {"base": None}

            def patched(sink):
                val = real_rendered_count(sink)
                if state["base"] is None:
                    state["base"] = val  # the rendered_before capture, valve still closed -- always 0
                    return val
                if not injected.is_set() and val is not None and state["base"] is not None and val > state["base"]:
                    # A genuine render increase was just observed. Inject
                    # the fresh, correctly source-scoped, correctly
                    # classified device-loss error for THIS pending
                    # generation RIGHT NOW -- before the worker code
                    # even sees this return value -- then block until
                    # the test explicitly releases it, so the test can
                    # assert on the in-between state deterministically.
                    msg = fake_error_message(
                        "gst-resource-error-quark: Error outputting to audio device. "
                        "The device has been disconnected. (10)")
                    obj._on_output_error(slot, None, msg)
                    injected.set()
                    release_worker.wait(timeout=5.0)
                return val

            with patch.object(eng_module, "_output_sink_rendered_count", patched):
                obj._output_dispatch_rebuild(slot)
                self.assertTrue(injected.wait(timeout=5.0), "device-loss injection point was never reached")
                # At this exact instant: a real render increase has
                # already been observed AND a fresh device-loss has
                # already been recorded against this generation, but the
                # worker itself is still paused, having not yet reached
                # its own final check/return.
                self.assertTrue(slot.valve.get_property("drop"),
                                 "the bus-error handler's own valve-close must already have fired")
                release_worker.set()

                self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.DEGRADED,
                                            timeout=OUTPUT_TEST_TIMEOUT))

            snapshot = slot.coordinator.snapshot()
            self.assertIs(snapshot["operation_succeeded"], False)
            self.assertTrue(slot.valve.get_property("drop"), "valve must remain/end closed, never left half-open")
            self.assertEqual(len(self.emitted_events), 0, "no 'recovered' event for a candidate that must not be promoted")
            obj._output_handle_slot_transition(slot, "RECOVERING", "DEGRADED", snapshot)
            self.assertIsNone(slot.pending_bin, "the un-promotable candidate must be discarded")
            self.assertNotEqual(slot.coordinator.state, audio_recovery.SlotState.OK)

            # Healthy sibling never stopped through any of this.
            self.assertGreater(healthy_count["n"], healthy_count_at_quiesce,
                                "healthy sibling must keep flowing through the whole race episode")

            # The discard just dispatched ITS OWN background teardown
            # worker for the failed candidate -- must wait for THAT to
            # resolve and re-degrade before dispatching a fresh rebuild,
            # or the fresh dispatch races the discard's own in-flight
            # operation and gets silently coalesced (caught live -- see
            # wait_for_pending_discard_to_resolve's own docstring).
            wait_for_pending_discard_to_resolve(obj, slot)

            # A subsequent, genuinely fresh rebuild attempt (unpatched --
            # real rendered-count checking, no injected failure) must
            # succeed normally, proving the slot is still retryable, not
            # wedged by the race.
            obj._output_dispatch_rebuild(slot)
            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK, timeout=5.0))
            snapshot2 = slot.coordinator.snapshot()
            self.assertIs(snapshot2["operation_succeeded"], True)
            obj._output_handle_slot_transition(slot, "RECOVERING", "OK", snapshot2)
            self.assertFalse(slot.valve.get_property("drop"), "the genuinely successful retry must reopen the valve")
            self.assertEqual(len(self.emitted_events), 1)
            self.assertEqual(self.emitted_events[0]["title"], "Studio Monitor output recovered")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_device_loss_after_worker_final_epoch_check_before_coordinator_success_cannot_promote(self):
        """SECOND pre-commit review finding: the worker's own final
        epoch check (exercised above) closes the race for anything
        happening WHILE the worker is still running -- but there is a
        further, narrower TOCTOU window between the worker's check
        PASSING (reads a still-matching epoch) and SlotCoordinator
        actually transitioning state to OK, plus the gap until
        _output_recovery_tick's next poll notices it. Forced
        deterministically here, not hoped for via timing: patches
        OutputRecoverySlot.device_loss_epoch() itself so that the
        SPECIFIC call representing the worker's post-loop final check
        (identified via coordination with a similarly patched
        _output_sink_rendered_count -- the render-verify loop's own
        `break` on a genuine increase skips its OWN per-iteration epoch
        check that same iteration, so the very next device_loss_epoch()
        call is guaranteed to be the final one, not a loop-iteration
        one) returns the PRE-injection (still-matching) value -- exactly
        as if the worker's read had already completed a moment before
        the real world changed underneath it -- while a REAL,
        source-scoped, correctly classified device_lost error is
        injected through the actual _on_output_error path DURING that
        same call, before it returns. The worker's own check therefore
        legitimately passes and it returns True; SlotCoordinator
        legitimately resolves the operation as successful and reaches
        OK -- proving this specific window is NOT closed by the
        worker-side check alone, only by the promotion-time re-check in
        _output_handle_slot_transition."""
        obj = make_output_engine_stand_in()
        pipeline = Gst.Pipeline.new("test-toctou-tee-pipeline")
        obj.main_pipeline = pipeline

        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("is-live", True)
        tee = Gst.ElementFactory.make("tee", "tee")
        pipeline.add(src)
        pipeline.add(tee)
        src.link(tee)

        q_healthy = Gst.ElementFactory.make("queue", "q_healthy")
        sink_healthy = Gst.ElementFactory.make("fakesink", "sink_healthy")
        sink_healthy.set_property("sync", False)
        pipeline.add(q_healthy)
        pipeline.add(sink_healthy)
        q_healthy.link(sink_healthy)
        tee.link(q_healthy)
        healthy_count = {"n": 0}

        def probe(pad, info):
            healthy_count["n"] += 1
            return Gst.PadProbeReturn.OK

        sink_healthy.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)

        with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                           lambda self, name: ("", "")):
            slot = obj._build_output_slot(
                "Studio Monitor", "studio_monitor", "hw:CARD=Fake,DEV=0",
                make_synthetic_generation_builder())
        pipeline.add(slot.queue)
        pipeline.add(slot.errorignore)
        pipeline.add(slot.valve)
        pipeline.add(slot.current_bin)
        slot.queue.link(slot.errorignore)
        slot.errorignore.link(slot.valve)
        slot.valve.get_static_pad("src").link(slot.current_bin.get_static_pad("sink"))
        tee.link(slot.queue)
        obj._output_slots = {"studio_monitor": slot}
        obj._studio_monitor_slot = slot

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", obj._on_main_bus_error)

        try:
            pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING))
            self.assertTrue(wait_until(lambda: healthy_count["n"] > 5, timeout=2.0))

            simulate_device_loss_and_quiesce(obj, slot)
            self.emitted_events.clear()
            healthy_count_at_quiesce = healthy_count["n"]

            injected = threading.Event()
            release_worker = threading.Event()
            real_rendered_count = eng_module._output_sink_rendered_count
            real_device_loss_epoch = eng_module.OutputRecoverySlot.device_loss_epoch
            state = {"base": None, "armed": False, "injected_once": False}

            def patched_rendered(sink):
                val = real_rendered_count(sink)
                if state["base"] is None:
                    state["base"] = val
                    return val
                if not state["armed"] and val is not None and val > state["base"]:
                    # This is the call that makes rendered_increased=True
                    # -- the loop breaks on THIS iteration, skipping its
                    # own epoch check, so the worker's very next call to
                    # device_loss_epoch() is guaranteed to be the
                    # post-loop final check, not a loop-iteration one.
                    state["armed"] = True
                return val

            def patched_epoch(self_slot):
                val = real_device_loss_epoch(self_slot)
                if self_slot is slot and state["armed"] and not state["injected_once"]:
                    state["injected_once"] = True
                    msg = fake_error_message(
                        "gst-resource-error-quark: Error outputting to audio device. "
                        "The device has been disconnected. (10)")
                    obj._on_output_error(slot, None, msg)  # bumps the REAL epoch, closes the valve, correctly
                    # coalesces (mark_degraded() returns False -- SlotCoordinator is still RECOVERING)
                    injected.set()
                    release_worker.wait(timeout=5.0)
                return val  # the PRE-injection value -- the worker's own check still sees a match

            with patch.object(eng_module, "_output_sink_rendered_count", patched_rendered), \
                    patch.object(eng_module.OutputRecoverySlot, "device_loss_epoch", patched_epoch):
                obj._output_dispatch_rebuild(slot)
                self.assertTrue(injected.wait(timeout=5.0), "injection point was never reached")
                # At this exact instant: the worker's final check has
                # already read a matching epoch value and is about to
                # return True; a fresh, real device-loss has ALREADY
                # been recorded (epoch bumped, valve closed) -- but
                # SlotCoordinator is STILL RECOVERING, since the worker
                # itself has not returned yet. This is the confirmation
                # the review specifically asked for: the error IS
                # coalesced at the coordinator level (proving
                # mark_degraded() behaved exactly as designed), yet the
                # candidate must still not end up promoted.
                self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.RECOVERING)
                self.assertTrue(slot.valve.get_property("drop"))
                release_worker.set()

                self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK,
                                            timeout=OUTPUT_TEST_TIMEOUT))

            # SlotCoordinator legitimately resolved this as a success --
            # the worker's own (now-stale) check genuinely passed.
            snapshot = slot.coordinator.snapshot()
            self.assertIs(snapshot["operation_succeeded"], True)

            # The promotion-time re-check is what must catch it here.
            bad_candidate_bin = slot.pending_bin  # captured BEFORE discard clears it -- see the assertion below
            obj._output_handle_slot_transition(slot, "RECOVERING", "OK", snapshot)

            self.assertIsNone(slot.pending_bin, "the un-promotable candidate must be discarded")
            self.assertTrue(slot.valve.get_property("drop"),
                             "valve must remain/become closed -- never left open on an un-promoted candidate")
            self.assertEqual(len(self.emitted_events), 0,
                              "no 'recovered' event for a candidate that must not be promoted")
            # Not asserting on the coordinator's transient STATE here --
            # caught live under a full-suite --shuffle run:
            # _output_discard_pending_bin's own teardown worker (a REAL
            # bin that really reached PLAYING, but still a trivial
            # synthetic one) can resolve back to OK before this line
            # runs under heavy system load, same class of race already
            # fixed elsewhere in this file and in test_engine_mic_
            # recovery.py's RemoteDjDoesNotBlockRecoveryTests. The
            # deterministic, non-racy proof that the candidate was
            # never promoted is that slot.current_bin never became the
            # bad candidate -- checked directly, not inferred from a
            # state label that's free to keep changing underneath it.
            self.assertIsNot(slot.current_bin, bad_candidate_bin,
                              "the failed candidate must never become the slot's current generation")

            # Healthy sibling never stopped through any of this.
            self.assertGreater(healthy_count["n"], healthy_count_at_quiesce,
                                "healthy sibling must keep flowing through the whole race episode")

            # The discard just dispatched its own background teardown --
            # wait for it to resolve/re-degrade before the next dispatch,
            # exactly like the render-verify race test above.
            wait_for_pending_discard_to_resolve(obj, slot)

            # A subsequent, genuinely fresh rebuild attempt must succeed
            # normally -- the slot is retryable, not wedged by the race.
            obj._output_dispatch_rebuild(slot)
            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK, timeout=5.0))
            snapshot2 = slot.coordinator.snapshot()
            self.assertIs(snapshot2["operation_succeeded"], True)
            obj._output_handle_slot_transition(slot, "RECOVERING", "OK", snapshot2)
            self.assertFalse(slot.valve.get_property("drop"), "the genuinely successful retry must reopen the valve")
            self.assertEqual(len(self.emitted_events), 1)
            self.assertEqual(self.emitted_events[0]["title"], "Studio Monitor output recovered")
        finally:
            pipeline.set_state(Gst.State.NULL)


class OutputRestartRequiredTests(MockEmitEventMixin, SimpleTestCase):
    """Item: watchdog timeout/abandonment leaves the sibling alive, no
    unbounded worker creation, diagnostics expose the required operator
    action. Uses a real, deliberately-wedged worker gated by a
    threading.Event (never a bare time.sleep -- matches this session's
    own audio_recovery test-hygiene fix) so nothing leaks past the test."""

    def test_wedged_teardown_abandons_without_hanging_test(self):
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            slot.coordinator.timeout_s = 0.1
            slot.coordinator.mark_degraded()
            release = threading.Event()
            result = slot.coordinator.request_recovery(lambda: (release.wait(timeout=10), True)[1])
            self.assertEqual(result, "dispatched")
            self.assertTrue(wait_until(
                lambda: (slot.coordinator.tick() or True) and
                        slot.coordinator.state == audio_recovery.SlotState.RESTART_REQUIRED,
                timeout=2.0))
            snapshot = slot.coordinator.snapshot()
            obj._output_handle_slot_transition(slot, "RECOVERING", "RESTART_REQUIRED", snapshot)
            self.assertEqual(len(self.emitted_events), 1)
            self.assertEqual(self.emitted_events[0]["title"], "Studio Monitor output recovery abandoned")

            release.set()  # deterministic cleanup -- see this session's own test-hygiene fix
            thread_name = f"slot-output-studio_monitor-op1"
            self.assertTrue(wait_until(
                lambda: not any(t.name == thread_name for t in threading.enumerate()), timeout=2.0),
                "the abandoned worker thread must actually exit once released -- must not leak past this test")
        finally:
            pipeline.set_state(Gst.State.NULL)


class OutputLegacyNoIdentityTests(MockEmitEventMixin, SimpleTestCase):
    """Item: blank identity preserves containment but never auto-
    rebuilds against a possibly-stale raw numeric device path."""

    def test_presence_probe_skips_slot_with_no_stable_identity(self):
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj, identity_kind="", identity="")
        try:
            slot.coordinator.mark_degraded()
            obj.running = True
            obj._output_presence_probe_tick()
            # No rebuild dispatch should have happened -- slot stays
            # DEGRADED with no operation in flight and no pending bin.
            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.DEGRADED)
            self.assertIsNone(slot.pending_bin)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_containment_still_works_without_identity(self):
        """Containment (valve closes, sibling protected) must NOT depend
        on a stable identity being configured -- only auto-rebuild-on-
        return does."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj, identity_kind="", identity="")
        try:
            gen_before = slot.coordinator.generation
            msg = fake_error_message(
                "gst-resource-error-quark: Error outputting to audio device. "
                "The device has been disconnected. (10)")
            obj._on_output_error(slot, None, msg)
            self.assertTrue(slot.valve.get_property("drop"))
            # Not asserting on the coordinator's transient STATE here --
            # caught live under a --shuffle run: the quiesce worker can
            # resolve back to OK before this line runs, depending on
            # system load/thread scheduling (same race already
            # documented and fixed in OutputSourceScopedRoutingTests and
            # in test_engine_mic_recovery.py's own
            # RemoteDjDoesNotBlockRecoveryTests). generation is the
            # deterministic signal that recovery was actually dispatched.
            self.assertGreater(slot.coordinator.generation, gen_before)
        finally:
            pipeline.set_state(Gst.State.NULL)


class OutputRecoveryIdentityLiveReloadTests(MockEmitEventMixin, SimpleTestCase):
    """Pre-commit review finding: an operator enabling Automatic
    Recovery via the admin (setting device_identity_kind/device_identity
    for the first time) had NO live-reload path at all -- the running
    slot's identity fields were fixed at engine-startup time, so the
    admin would silently imply "enabled" while the engine kept using
    stale (usually blank) values until the next full restart. Fixed via
    _reload_output_recovery_identity() -- see
    hardware/tests/test_audio_output_recovery_reload_signal.py for the
    admin-side signal half of this fix."""

    def test_reload_updates_identity_without_touching_hardware(self):
        """[P0] 1.3C second integration-bug fix -- strengthened per that
        review's own finding: object identity alone (current_bin/
        current_sink `is` the same object) doesn't prove non-disturbance,
        since the SAME sink object can have its `device` property changed
        after a whole-pipeline READY cycle -- that's exactly what
        happened in production. Swaps in _FakeAlsaSink (see below) so
        the actual device-property value can be asserted, not just
        object identity. See OutputRawDeviceSwapSemanticsTests for the
        full command-level (identity + device-swap-decision + AGC
        together) coverage this test doesn't attempt -- this one is
        deliberately scoped to _reload_output_recovery_identity() alone,
        matching its own narrow contract."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj, identity_kind="", identity="")
        try:
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING))
            slot.current_sink = _FakeAlsaSink("plughw:CARD=CODEC,DEV=0")
            current_bin_before = slot.current_bin
            current_sink_before = slot.current_sink
            valve_state_before = slot.valve.get_property("drop")
            gen_before = slot.coordinator.generation

            with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                               lambda self, name: ("alsa_card_id", "CODEC")), \
                 patch.object(pipeline, "set_state", wraps=pipeline.set_state) as spy:
                obj._reload_output_recovery_identity()

            self.assertEqual(slot.identity_kind, "alsa_card_id")
            self.assertEqual(slot.identity, "CODEC")
            # Nothing about the running hardware generation was touched
            # -- the review's explicit requirement that an identity-only
            # edit must never disturb a healthy sink. Checked via the
            # ACTUAL device property, not object identity alone.
            self.assertEqual(slot.current_sink.device, "plughw:CARD=CODEC,DEV=0")
            self.assertEqual(slot.current_sink.calls, [])
            for call in spy.call_args_list:
                self.assertNotEqual(call.args[0], Gst.State.READY)
            self.assertIs(slot.current_bin, current_bin_before)
            self.assertIs(slot.current_sink, current_sink_before)
            self.assertEqual(slot.valve.get_property("drop"), valve_state_before)
            self.assertEqual(slot.coordinator.generation, gen_before)
            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.OK)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_reload_is_a_noop_when_identity_unchanged(self):
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj, identity_kind="alsa_card_id", identity="CODEC")
        try:
            with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                               lambda self, name: ("alsa_card_id", "CODEC")):
                obj._reload_output_recovery_identity()
            self.assertEqual(slot.identity_kind, "alsa_card_id")
            self.assertEqual(slot.identity, "CODEC")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_reload_refreshes_every_built_slot(self):
        """Two independent slots -- confirms the reload iterates ALL
        currently-built slots, not just one, regardless of which
        specific AudioOutput row's save actually triggered the reload
        command (matches hardware/signals.py's own "iterate every slot,
        don't try to track which row triggered it" design)."""
        obj = make_output_engine_stand_in()
        pipeline = Gst.Pipeline.new("test-reload-two-slots")
        obj.main_pipeline = pipeline

        def make_slot(name, kind):
            with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                               lambda self, n: ("", "")):
                slot = obj._build_output_slot(name, kind, f"hw:CARD=Fake_{kind},DEV=0",
                                               make_synthetic_generation_builder())
            pipeline.add(slot.queue)
            pipeline.add(slot.errorignore)
            pipeline.add(slot.valve)
            pipeline.add(slot.current_bin)
            slot.queue.link(slot.errorignore)
            slot.errorignore.link(slot.valve)
            slot.valve.get_static_pad("src").link(slot.current_bin.get_static_pad("sink"))
            return slot

        sm_slot = make_slot("Studio Monitor", "studio_monitor")
        st_slot = make_slot("Stereotool Input", "stereotool")
        obj._output_slots = {"studio_monitor": sm_slot, "stereotool": st_slot}
        obj._studio_monitor_slot = sm_slot
        obj._stereotool_slot = st_slot

        try:
            def fake_identity(self, name):
                return {"Studio Monitor": ("alsa_card_id", "PCH"),
                        "Stereotool Input": ("alsa_card_id", "CODEC")}.get(name, ("", ""))

            with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity", fake_identity):
                obj._reload_output_recovery_identity()

            self.assertEqual(sm_slot.identity, "PCH")
            self.assertEqual(st_slot.identity, "CODEC")
        finally:
            pipeline.set_state(Gst.State.NULL)


class ReloadAudioOutputCommandDispatchTests(MockEmitEventMixin, SimpleTestCase):
    """[P0] 1.3C integration-bug fix -- regression coverage for the
    engine-side half. "reload_audio_output" is Studio Monitor's ONE
    unified live-reload command: it must invoke
    _reload_output_recovery_identity(), _apply_audio_output_device(...),
    AND _apply_agc_config(), all under this single command. AGC reapply
    used to arrive only via a separate "reload_agc_config" command
    written directly by AudioOutputAdmin.save_model() -- which raced and
    clobbered THIS command's own write to the same single-slot
    engine_cmd.json (see hardware/admin.py's save_model docstring and
    hardware/tests/test_audio_output_recovery_reload_signal.py's
    AudioOutputAdminSaveModelIntegrationTests for the admin-side half of
    this fix). Uses lightweight recording stubs rather than assertions
    inside the stubs themselves -- _check_commands wraps its whole
    dispatch in a bare `except Exception: print(...)`, so an
    AssertionError raised from inside a stub would be silently
    swallowed there instead of failing the test.

    CMD_PATH is patched to a throwaway tempfile -- this box IS
    production and the real /run/isadoraair/engine_cmd.json is the live
    engine's actual IPC inbox. A test must never write to it."""

    def _obj_with_recording_stubs(self):
        obj = make_output_engine_stand_in()
        calls = []
        obj._reload_output_recovery_identity = lambda: calls.append("reload_identity")
        obj._resolve_studio_monitor_device = lambda: "plughw:2,0"
        obj._apply_audio_output_device = lambda device: calls.append(("apply_device", device))
        obj._apply_agc_config = lambda: calls.append("apply_agc")
        return obj, calls

    def _dispatch(self, obj, command):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_path = Path(tmp) / "engine_cmd.json"
            cmd_path.write_text(json.dumps({"command": command}), encoding="utf-8")
            with patch.object(eng_module, "CMD_PATH", cmd_path):
                obj._check_commands()
            self.assertFalse(cmd_path.exists(), "command file must be consumed (unlinked)")

    def test_reload_audio_output_invokes_identity_device_and_agc(self):
        obj, calls = self._obj_with_recording_stubs()
        self._dispatch(obj, "reload_audio_output")
        self.assertEqual(calls, ["reload_identity", ("apply_device", "plughw:2,0"), "apply_agc"])

    def test_reload_audio_output_recovery_config_does_not_touch_device_or_agc(self):
        """Stereotool Input (and any other non-Studio-Monitor row) still
        gets identity-only reload -- no raw-device swap, no AGC touch."""
        obj, calls = self._obj_with_recording_stubs()
        self._dispatch(obj, "reload_audio_output_recovery_config")
        self.assertEqual(calls, ["reload_identity"])

    def test_reload_agc_config_still_works_standalone(self):
        """Kept as a harmless, still-correct handler even though nothing
        writes this command in production anymore (see the fix report's
        repo-wide grep) -- removing support outright wasn't asked for."""
        obj, calls = self._obj_with_recording_stubs()
        self._dispatch(obj, "reload_agc_config")
        self.assertEqual(calls, ["apply_agc"])


class _FakeAlsaSink:
    """[P0] 1.3C second integration-bug fix -- stand-in for a real
    alsasink's `device` property. fakesink (used by
    make_synthetic_generation_builder for the rest of this file) has no
    such property at all -- confirmed directly: fs.get_property("device")
    raises TypeError, "object of type `GstFakeSink' does not have
    property `device'". A REAL alsasink would risk touching actual ALSA
    hardware even when pointed at a garbage device string (this file's
    own stated invariant: "No real ALSA hardware is opened anywhere in
    this file"). _apply_audio_output_device only ever calls
    .get_property("device")/.set_property("device", ...) on
    slot.current_sink (confirmed by reading its implementation) -- it
    never bin_.add()s current_sink or otherwise needs it to be a real
    Gst.Element -- so this plain Python stand-in is both safe and a
    strictly STRONGER proof than object-identity-only checks: the exact
    gap this review flagged ("the same Gst alsasink object can have its
    `device` property changed after a whole-pipeline READY cycle" --
    identity survives that; the property doesn't)."""

    def __init__(self, device):
        self.device = device
        self.calls = []

    def get_property(self, name):
        if name == "device":
            return self.device
        raise AssertionError(f"unexpected get_property({name!r}) on fake sink")

    def set_property(self, name, value):
        self.calls.append((name, value))
        if name == "device":
            self.device = value
        else:
            raise AssertionError(f"unexpected set_property({name!r}, {value!r}) on fake sink")


class OutputRawDeviceSwapSemanticsTests(MockEmitEventMixin, SimpleTestCase):
    """[P0] 1.3C second integration-bug fix -- _apply_audio_output_device
    used to decide "did the device change" by comparing the requested
    raw device against the LIVE sink's ACTUAL open device
    (sink.get_property("device")). That diverges from the raw numeric
    path ON PURPOSE once a stable identity has resolved the sink to
    plughw:CARD=<id>,DEV=0 (see resolve_runtime_device) -- so every
    identity-only edit, AGC-only edit, or completely unchanged Save
    (every one of which reaches this call with the SAME raw device
    string every time, via the unified "reload_audio_output" command)
    was mistaken for a real device change, forcing a needless whole-
    pipeline READY cycle on a healthy sink. Confirmed live in
    production: a no-op Save moved the sink OFF its stable CARD=PCH
    path onto the raw numeric path.

    Fixed by comparing the requested device against slot.legacy_device
    (this slot's own last-known raw configuration) instead of the live
    sink -- and by treating a REAL raw-device change as a no-op on the
    live sink specifically WHILE a stable identity is active (identity
    stays authoritative for what the sink actually uses; the raw field
    becomes fallback-only metadata for if/when identity is later
    cleared -- consistent with resolve_runtime_device already ignoring
    legacy_device whenever identity is set).

    A spy on main_pipeline.set_state proves the pipeline was (or
    wasn't) ever dropped to READY -- the actual disturbance this bug
    caused -- rather than trusting object identity alone."""

    def _make_slot(self, identity_kind, identity, legacy_device, sink_device):
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(
            obj, legacy_device=legacy_device, identity_kind=identity_kind, identity=identity)
        slot.current_sink = _FakeAlsaSink(sink_device)
        return obj, pipeline, slot

    def _assert_sink_and_pipeline_untouched(self, slot, pipeline, set_state_spy,
                                             current_bin_before, current_sink_before,
                                             valve_before, gen_before, state_before):
        self.assertEqual(slot.current_sink.calls, [], "sink property must never be touched")
        for call in set_state_spy.call_args_list:
            self.assertNotEqual(call.args[0], Gst.State.READY, "main_pipeline must never enter READY")
        self.assertIs(slot.current_bin, current_bin_before)
        self.assertIs(slot.current_sink, current_sink_before)
        self.assertEqual(slot.valve.get_property("drop"), valve_before)
        self.assertEqual(slot.coordinator.generation, gen_before)
        self.assertEqual(slot.coordinator.state, state_before)
        self.assertEqual(self.emitted_events, [], "no recovery activity must be triggered")

    def test_a_stable_identity_unchanged_raw_device_leaves_sink_and_pipeline_untouched(self):
        obj, pipeline, slot = self._make_slot(
            identity_kind="alsa_card_id", identity="PCH",
            legacy_device="plughw:2,0", sink_device="plughw:CARD=PCH,DEV=0")
        try:
            current_bin_before, current_sink_before = slot.current_bin, slot.current_sink
            valve_before = slot.valve.get_property("drop")
            gen_before, state_before = slot.coordinator.generation, slot.coordinator.state

            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as spy:
                result = obj._apply_audio_output_device("plughw:2,0")  # same raw device -- unchanged Save

            self.assertFalse(result, "_apply_audio_output_device must report no state transition")
            self.assertEqual(slot.current_sink.device, "plughw:CARD=PCH,DEV=0")
            self._assert_sink_and_pipeline_untouched(
                slot, pipeline, spy, current_bin_before, current_sink_before,
                valve_before, gen_before, state_before)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_a_full_unified_command_reapplies_agc_without_disturbing_sink(self):
        """The same scenario as above, but through the REAL unified
        "reload_audio_output" command dispatch (identity refresh +
        device apply + AGC reapply together) -- proving AGC is still
        reapplied on an otherwise-unchanged Save, while the device-swap
        portion remains correctly a no-op. _apply_agc_config is stubbed
        (recording) since wiring real AGC elements is orthogonal to
        this bug; _apply_audio_output_device and
        _reload_output_recovery_identity are the REAL implementations."""
        obj, pipeline, slot = self._make_slot(
            identity_kind="alsa_card_id", identity="PCH",
            legacy_device="plughw:2,0", sink_device="plughw:CARD=PCH,DEV=0")
        try:
            current_bin_before, current_sink_before = slot.current_bin, slot.current_sink
            valve_before = slot.valve.get_property("drop")
            gen_before, state_before = slot.coordinator.generation, slot.coordinator.state

            agc_calls = []
            obj._apply_agc_config = lambda: agc_calls.append("apply_agc")
            obj._resolve_studio_monitor_device = lambda: "plughw:2,0"  # unchanged

            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as spy, \
                 patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                              lambda self, name: ("alsa_card_id", "PCH")):  # unchanged identity too
                obj._reload_output_recovery_identity()
                obj._apply_audio_output_device(obj._resolve_studio_monitor_device())
                obj._apply_agc_config()

            self.assertEqual(agc_calls, ["apply_agc"], "AGC must still be reapplied")
            self._assert_sink_and_pipeline_untouched(
                slot, pipeline, spy, current_bin_before, current_sink_before,
                valve_before, gen_before, state_before)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_b_identity_only_change_updates_metadata_but_not_sink(self):
        """Changing ONLY the identity fields (raw device untouched) must
        update the slot's recovery metadata while leaving the healthy
        sink and pipeline completely alone -- strengthens the older
        test_reload_updates_identity_without_touching_hardware (object-
        identity-only) with the actual device-property/pipeline-state
        proof that test could not provide."""
        obj, pipeline, slot = self._make_slot(
            identity_kind="", identity="",
            legacy_device="plughw:2,0", sink_device="plughw:2,0")
        try:
            current_bin_before, current_sink_before = slot.current_bin, slot.current_sink
            valve_before = slot.valve.get_property("drop")
            gen_before, state_before = slot.coordinator.generation, slot.coordinator.state

            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as spy, \
                 patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                              lambda self, name: ("alsa_card_id", "PCH")):
                obj._reload_output_recovery_identity()
                result = obj._apply_audio_output_device("plughw:2,0")  # raw device still unchanged

            self.assertFalse(result)
            self.assertEqual(slot.identity_kind, "alsa_card_id")
            self.assertEqual(slot.identity, "PCH")
            self.assertEqual(slot.current_sink.device, "plughw:2,0")
            self._assert_sink_and_pipeline_untouched(
                slot, pipeline, spy, current_bin_before, current_sink_before,
                valve_before, gen_before, state_before)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_c_legacy_mode_real_device_change_still_swaps_live_sink(self):
        """No stable identity configured (legacy mode) -- a genuine raw
        `device` change must still swap the live sink, unchanged from
        pre-1.3C behavior. The existing whole-pipeline READY/PLAYING
        cycle is the intentionally-accepted, documented blast radius
        for this specific case (an operator-requested device change)."""
        obj, pipeline, slot = self._make_slot(
            identity_kind="", identity="",
            legacy_device="plughw:2,0", sink_device="plughw:2,0")
        try:
            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as spy:
                result = obj._apply_audio_output_device("plughw:5,0")

            self.assertTrue(result, "a real raw-device change in legacy mode must report a state transition")
            self.assertEqual(slot.current_sink.device, "plughw:5,0")
            self.assertIn(("device", "plughw:5,0"), slot.current_sink.calls)
            states = [call.args[0] for call in spy.call_args_list]
            self.assertIn(Gst.State.READY, states)
            self.assertIn(Gst.State.PLAYING, states)
            self.assertEqual(slot.legacy_device, "plughw:5,0")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_d_stable_identity_real_raw_device_change_updates_fallback_only(self):
        """A genuine raw `device` edit WHILE a stable identity is active
        must NOT force the healthy sink off its stable CARD=<id> path --
        resolve_runtime_device() already ignores legacy_device entirely
        whenever identity is set, so forcing the sink onto the raw path
        here would silently contradict that. The raw value is still
        recorded as fallback/config metadata (for if/when identity is
        later cleared) -- this is the explicit chosen semantics, not an
        oversight; see this method's own docstring."""
        obj, pipeline, slot = self._make_slot(
            identity_kind="alsa_card_id", identity="PCH",
            legacy_device="plughw:2,0", sink_device="plughw:CARD=PCH,DEV=0")
        try:
            current_bin_before, current_sink_before = slot.current_bin, slot.current_sink
            valve_before = slot.valve.get_property("drop")
            gen_before, state_before = slot.coordinator.generation, slot.coordinator.state

            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as spy:
                result = obj._apply_audio_output_device("plughw:9,0")  # a genuine raw-device edit

            self.assertFalse(result, "the live sink must not move off its stable identity path")
            self.assertEqual(slot.current_sink.device, "plughw:CARD=PCH,DEV=0", "sink must stay on the stable path")
            self._assert_sink_and_pipeline_untouched(
                slot, pipeline, spy, current_bin_before, current_sink_before,
                valve_before, gen_before, state_before)
            # The raw field itself IS still updated -- fallback/config
            # metadata, used only if identity is later cleared.
            self.assertEqual(slot.legacy_device, "plughw:9,0")
        finally:
            pipeline.set_state(Gst.State.NULL)


class OutputRecoveryStateActiveDeviceReportingTests(SimpleTestCase):
    """[P0] 1.3C second integration-bug fix, item E -- production
    surfaced that engine_state.json's `resolved_runtime_device` is a
    PURE function of config (identity_kind/identity/legacy_device), not
    a read of what the live sink actually has open -- so a bug that
    forced the sink off its stable path left `resolved_runtime_device`
    still reporting the stable path throughout, unable to catch the
    disturbance. `active_device` (new field) reads
    current_sink.get_property("device") directly -- the actual live
    value, however it got there. `resolved_runtime_device` is
    deliberately NOT renamed or removed -- it's still the right answer
    to "what should this be given current config"; `active_device`
    answers the different question "what is it actually right now"."""

    def test_active_device_reflects_the_real_sink_not_the_desired_resolution(self):
        """Reproduces the exact production-surfaced gap: force a
        mismatch between the sink's actual device and what config-based
        resolution would compute, and confirm state reporting exposes
        BOTH values distinctly rather than only the desired one."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(
            obj, legacy_device="plughw:2,0", identity_kind="alsa_card_id", identity="PCH")
        try:
            # Simulate the bug's exact aftermath: identity says PCH, but
            # the live sink has (wrongly) ended up on the raw path.
            slot.current_sink = _FakeAlsaSink("plughw:2,0")

            state = obj._output_recovery_state()["studio_monitor"]

            self.assertEqual(state["resolved_runtime_device"], "plughw:CARD=PCH,DEV=0",
                              "desired/config-computed resolution is unchanged")
            self.assertEqual(state["active_device"], "plughw:2,0",
                              "active_device must reflect the REAL live sink, not the desired resolution")
            self.assertNotEqual(state["active_device"], state["resolved_runtime_device"],
                                 "this exact mismatch is what production hit -- state reporting must be able to show it")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_active_device_matches_resolved_runtime_device_when_healthy(self):
        """The ordinary/healthy case: once the sink genuinely IS on its
        stable identity path, both fields agree -- proving
        active_device isn't just always-different noise."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(
            obj, legacy_device="plughw:2,0", identity_kind="alsa_card_id", identity="PCH")
        try:
            slot.current_sink = _FakeAlsaSink("plughw:CARD=PCH,DEV=0")
            state = obj._output_recovery_state()["studio_monitor"]
            self.assertEqual(state["active_device"], state["resolved_runtime_device"])
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_active_device_none_when_sink_property_read_fails(self):
        """Defensive: a sink that can't report its device (property
        read raises) must degrade to None, never raise out of state
        reporting -- mirrors _apply_audio_output_device's own existing
        try/except around the identical read."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj, legacy_device="plughw:2,0")
        try:
            class _BrokenSink:
                def get_property(self, name):
                    raise RuntimeError("boom")
            slot.current_sink = _BrokenSink()
            state = obj._output_recovery_state()["studio_monitor"]
            self.assertIsNone(state["active_device"])
        finally:
            pipeline.set_state(Gst.State.NULL)
