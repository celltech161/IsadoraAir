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

from django.test import SimpleTestCase, TestCase

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



class _DeviceAwareSink:
    """Expose a synthetic sink's real stats plus the requested device."""

    def __init__(self, sink, device):
        self._sink = sink
        self.device = device

    def get_property(self, name):
        if name == "device":
            return self.device
        return self._sink.get_property(name)

    def get_static_pad(self, name):
        return self._sink.get_static_pad(name)


def make_device_aware_generation_builder(failing_devices=None, calls=None):
    failing_devices = failing_devices if failing_devices is not None else set()
    calls = calls if calls is not None else []

    def build(device):
        calls.append(device)
        if device in failing_devices:
            bin_ = Gst.Bin.new(f"failed_gen_{int(time.time() * 1_000_000)}")
            gate = Gst.ElementFactory.make("valve", None)
            gate.set_property("drop", True)
            sink = Gst.ElementFactory.make("fakesink", None)
            sink.set_property("sync", False)
            sink.set_property("async", False)
            bin_.add(gate)
            bin_.add(sink)
            gate.link(sink)
            ghost = Gst.GhostPad.new("sink", gate.get_static_pad("sink"))
            ghost.set_active(True)
            bin_.add_pad(ghost)
        else:
            bin_, sink = make_synthetic_generation_builder()(device)
        return bin_, _DeviceAwareSink(sink, device)
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


def fake_error_message(message_text, debug_text="", src=None):
    """`src` defaults to None (every pre-existing caller calls
    _on_output_error directly, bypassing the ownership routing that
    reads message.src, so they never needed one). [P0] 1.3C physical-
    acceptance-failure fix: tests that need to exercise the REAL
    ownership routing (_on_main_bus_error/_output_slot_owns_message_src)
    pass a real Gst element here -- src stays a plain attribute (not a
    method) to match GStreamer's own Gst.Message.src shape."""
    class _Err:
        def __str__(self_inner):
            return message_text

    class _Msg:
        def parse_error(self_inner):
            return _Err(), debug_text

    msg = _Msg()
    msg.src = src
    return msg


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



def complete_successful_retarget(obj, slot, timeout=5.0):
    """Drive both asynchronous coordinator completions for a retarget."""
    if not wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK,
                      timeout=timeout):
        raise AssertionError("retarget teardown did not resolve to OK")
    obj._output_handle_slot_transition(
        slot, "RECOVERING", "OK", slot.coordinator.snapshot())
    if not wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK,
                      timeout=timeout):
        raise AssertionError("retarget candidate did not resolve to OK")
    obj._output_handle_slot_transition(
        slot, "RECOVERING", "OK", slot.coordinator.snapshot())
    if slot.current_bin is None or slot.current_sink is None:
        raise AssertionError("retarget candidate was not promoted")


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

    def test_studio_monitor_generation_applies_branch_safe_sink_properties(self):
        """[P0] 1.3C physical-acceptance-failure fix -- async is now
        explicitly False (was left at alsasink's True default, which
        the empirical preroll harness proved can never reach PLAYING
        behind the recovery choreography's closed valve -- see
        _build_studio_monitor_hw_generation's own docstring). Physical
        P4/P5 capture later proved sync=True can accept nonzero buffers
        while producing only analog noise; sync=False restored physical
        signal on both UCA222 directions. latency-time is checked against
        alsasink's own real default (10000, confirmed directly via
        gst-inspect) rather than StereoTool's tuned 20000, proving
        Studio Monitor was NOT accidentally given StereoTool's whole
        property set -- only the independently justified base-sink changes.
        (buffer-time is NOT checked here: alsasink's own default
        happens to already equal StereoTool's tuned 200000, so that
        property alone can't distinguish "got StereoTool's value" from
        "kept its own default" -- latency-time is the property that
        actually proves it.)"""
        obj = make_output_engine_stand_in()
        bin_, sink = obj._build_studio_monitor_hw_generation("hw:CARD=Fake,DEV=0")
        try:
            self.assertFalse(sink.get_property("sync"), "physical P5 fix must survive every generation")
            self.assertFalse(sink.get_property("async"), "async must be explicitly False (the fix)")
            self.assertEqual(sink.get_property("latency-time"), 10000,
                              "must be alsasink's own default, not StereoTool's tuned 20000")
        finally:
            bin_.set_state(Gst.State.NULL)

    def test_studio_monitor_generation_owns_fresh_processing_tail(self):
        """Gate A regression: a retarget replaces stateful processing too."""
        obj = make_output_engine_stand_in()
        first_bin, first_sink = obj._build_studio_monitor_hw_generation(
            "hw:CARD=Fake,DEV=0")
        second_bin, second_sink = obj._build_studio_monitor_hw_generation(
            "hw:CARD=Fake,DEV=0")
        try:
            for bin_, sink in ((first_bin, first_sink), (second_bin, second_sink)):
                self.assertIsNotNone(bin_.get_by_name("agc_dynamic"))
                self.assertIsNotNone(bin_.get_by_name("agc_makeup"))
                self.assertIsNotNone(bin_.get_by_name("agc_limiter"))
                self.assertIs(sink.get_parent(), bin_)
            for name in ("agc_dynamic", "agc_makeup", "agc_limiter"):
                self.assertIsNot(
                    first_bin.get_by_name(name), second_bin.get_by_name(name),
                    f"{name} must be replaced with the generation")
        finally:
            first_bin.set_state(Gst.State.NULL)
            second_bin.set_state(Gst.State.NULL)

    def test_real_studio_processing_generation_passes_nonzero_samples(self):
        """The production AGC/limiter topology must not manufacture silence."""
        obj = make_output_engine_stand_in()
        obj._studio_monitor_agc_settings = {
            "enabled": True,
            "ratio": 10.0,
            "threshold": 0.9,
            "soft_knee": True,
            "makeup_gain_db": 0.0,
        }
        bin_, sink = obj._build_studio_monitor_hw_generation(
            None, sink_factory="appsink")
        sink.set_property("sync", False)
        sink.set_property("max-buffers", 1)
        pipeline = Gst.Pipeline.new("studio-processing-content-regression")
        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("is-live", True)
        src.set_property("volume", 0.2)
        pipeline.add(src)
        pipeline.add(bin_)
        src.link(bin_)
        try:
            pipeline.set_state(Gst.State.PLAYING)
            sample = sink.emit("try-pull-sample", 2 * Gst.SECOND)
            self.assertIsNotNone(sample, "processing generation must deliver a sample")
            buffer = sample.get_buffer()
            ok, map_info = buffer.map(Gst.MapFlags.READ)
            self.assertTrue(ok)
            try:
                self.assertTrue(
                    any(byte != 0 for byte in map_info.data),
                    "nonzero input must remain nonzero after AGC/limiter")
            finally:
                buffer.unmap(map_info)
        finally:
            pipeline.set_state(Gst.State.NULL)


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
        release_teardown = threading.Event()
        old_bin = slot.current_bin
        original_set_state = old_bin.set_state

        def held_set_state(state):
            release_teardown.wait(timeout=2.0)
            return original_set_state(state)

        try:
            gen_before = slot.coordinator.generation
            epoch_before = slot.device_loss_epoch()
            msg = fake_error_message(
                "gst-resource-error-quark: Error outputting to audio device. "
                "The device has been disconnected. (10)")
            # Keep the guarded teardown operation deterministically in
            # flight for the whole synthetic burst. Production's real bus
            # router also rejects messages from the synchronously retired
            # old bin; this direct-handler unit test intentionally bypasses
            # that ownership layer and therefore must hold the operation.
            with patch.object(old_bin, "set_state", side_effect=held_set_state):
                for _ in range(62):
                    obj._on_output_error(slot, None, msg)
                self.assertEqual(slot.coordinator.generation, gen_before + 1,
                                  "62 repeated device-loss messages must dispatch exactly ONE recovery cycle")
                self.assertEqual(len(self.emitted_events), 1)
                self.assertEqual(slot.device_loss_epoch(), epoch_before + 62,
                                  "the epoch marker must bump on every classified message, coalesced or not")
        finally:
            release_teardown.set()
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
            self.assertIsNone(slot.current_bin, "the rejected generation must never become current")
            # The bounded discard worker is allowed to finish immediately;
            # OK here means only "teardown returned", not that a candidate
            # was promoted. wait_for_pending_discard_to_resolve below turns
            # that completion back into the retryable DEGRADED state.
            self.assertIn(
                slot.coordinator.state,
                (audio_recovery.SlotState.RECOVERING, audio_recovery.SlotState.OK),
            )

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
        obj._reload_output_recovery_identity = lambda: calls.append("reload_identity") or {"studio_monitor"}
        obj._resolve_studio_monitor_device = lambda: "plughw:2,0"
        obj._apply_audio_output_device = lambda device, **kwargs: calls.append(
            ("apply_device", device, kwargs))
        obj._apply_agc_config = lambda: calls.append("apply_agc")
        return obj, calls

    def _dispatch(self, obj, command):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_path = Path(tmp) / "engine_cmd.json"
            cmd_path.write_text(json.dumps({"command": command}), encoding="utf-8")
            with patch.object(eng_module, "CMD_PATH", cmd_path):
                obj._check_commands()
            self.assertFalse(cmd_path.exists(), "command file must be consumed (unlinked)")

    def test_reload_audio_output_caches_agc_before_identity_device_swap(self):
        """A replacement built immediately must inherit the new AGC values."""
        obj, calls = self._obj_with_recording_stubs()
        self._dispatch(obj, "reload_audio_output")
        self.assertEqual(calls, [
            "apply_agc",
            "reload_identity",
            ("apply_device", "plughw:2,0", {"identity_changed": True}),
        ])

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

    A spy on main_pipeline.set_state proves the pipeline is never dropped
    to READY; a real legacy edit now replaces only the monitor branch."""

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
        `device` change replaces only the Studio Monitor generation."""
        obj, pipeline, slot = self._make_slot(
            identity_kind="", identity="",
            legacy_device="plughw:2,0", sink_device="plughw:2,0")
        try:
            old_bin = slot.current_bin
            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as spy:
                result = obj._apply_audio_output_device("plughw:5,0")
                complete_successful_retarget(obj, slot)

            self.assertTrue(result, "a real raw-device change in legacy mode must be accepted")
            self.assertIsNot(slot.current_bin, old_bin)
            states = [call.args[0] for call in spy.call_args_list]
            self.assertNotIn(Gst.State.READY, states)
            self.assertNotIn(Gst.State.PLAYING, states)
            self.assertEqual(slot.legacy_device, "plughw:5,0")
            self.assertFalse(slot.valve.get_property("drop"))
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_stable_identity_swap_keeps_stereotool_playing_and_continuous(self):
        """Production-shaped PCH->CODEC retarget leaves StereoTool untouched."""
        obj = make_output_engine_stand_in()
        pipeline = Gst.Pipeline.new("standalone-device-swap-harness")
        obj.main_pipeline = pipeline
        monitor_builder = make_device_aware_generation_builder()
        sibling_builder = make_synthetic_generation_builder()
        with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                          lambda self, name: (("alsa_card_id", "PCH")
                                             if name == "Studio Monitor" else ("", ""))):
            monitor = obj._build_output_slot(
                "Studio Monitor", "studio_monitor", "device-a", monitor_builder)
            stereotool = obj._build_output_slot(
                "Stereotool Input", "stereotool", "stereotool-device", sibling_builder)
        obj._studio_monitor_slot = monitor
        obj._stereotool_slot = stereotool
        obj._output_slots = {"studio_monitor": monitor, "stereotool": stereotool}

        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("is-live", True)
        tee = Gst.ElementFactory.make("tee", None)
        for element in (src, tee,
                        monitor.queue, monitor.errorignore, monitor.valve, monitor.current_bin,
                        stereotool.queue, stereotool.errorignore, stereotool.valve, stereotool.current_bin):
            pipeline.add(element)
        src.link(tee)
        for slot in (monitor, stereotool):
            tee.link(slot.queue)
            slot.queue.link(slot.errorignore)
            slot.errorignore.link(slot.valve)
            slot.valve.get_static_pad("src").link(slot.current_bin.get_static_pad("sink"))

        sibling_buffers = []
        sibling_states = []
        def sibling_probe(pad, info):
            sibling_buffers.append(time.monotonic())
            sibling_states.append((pipeline.get_state(0)[1], stereotool.current_bin.get_state(0)[1]))
            return Gst.PadProbeReturn.OK
        stereotool.current_sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, sibling_probe)
        try:
            pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(wait_until(lambda: len(sibling_buffers) >= 5))
            before = len(sibling_buffers)
            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as state_spy, \
                 patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                              lambda self, name: (("alsa_card_id", "CODEC")
                                                 if name == "Studio Monitor" else ("", ""))):
                changes = obj._reload_output_recovery_identity()
                self.assertTrue(obj._apply_audio_output_device(
                    "device-a", identity_changed="studio_monitor" in changes))
                complete_successful_retarget(obj, monitor)
                self.assertTrue(wait_until(lambda: len(sibling_buffers) > before + 5))

            self.assertEqual(state_spy.call_args_list, [], "parent pipeline state must never be set")
            _, sibling_state, _ = stereotool.current_bin.get_state(0)
            _, pipeline_state, _ = pipeline.get_state(0)
            self.assertEqual(sibling_state, Gst.State.PLAYING)
            self.assertEqual(pipeline_state, Gst.State.PLAYING)
            self.assertFalse(stereotool.valve.get_property("drop"))
            self.assertTrue(all(a == Gst.State.PLAYING and b == Gst.State.PLAYING
                                for a, b in sibling_states[before:]))
            intervals = [b - a for a, b in zip(sibling_buffers, sibling_buffers[1:])]
            max_gap = max(intervals)
            print(f"  StereoTool success-retarget max inter-buffer gap: {max_gap:.6f}s")
            self.assertLess(max_gap, 0.25, "StereoTool buffer production must remain continuous")
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


class OutputIntentionalStableIdentityRetargetTests(MockEmitEventMixin, SimpleTestCase):
    """Explicit PCH/CODEC live-retarget coverage using synthetic sinks."""

    def _make_slot(self, identity="PCH", failing_devices=None, calls=None):
        obj = make_output_engine_stand_in()
        builder = make_device_aware_generation_builder(failing_devices, calls)
        pipeline, slot = build_slot_in_pipeline(
            obj, identity_kind="alsa_card_id", identity=identity,
            legacy_device="plughw:2,0", build_generation_fn=builder)
        obj.running = True
        return obj, pipeline, slot

    def _request_identity(self, obj, identity):
        with patch.object(
                eng_module.PlaybackEngine, "_resolve_output_device_identity",
                lambda self, name: ("alsa_card_id", identity)):
            changed = obj._reload_output_recovery_identity()
        self.assertEqual(changed, {"studio_monitor"})
        return obj._apply_audio_output_device(
            obj._studio_monitor_slot.legacy_device,
            identity_changed="studio_monitor" in changed)

    def test_pch_to_alternate_and_back_updates_all_slot_bookkeeping(self):
        calls = []
        obj, pipeline, slot = self._make_slot(calls=calls)
        try:
            old_bin = slot.current_bin
            old_child = old_bin.iterate_elements().next()[1]
            epoch_before = slot.device_loss_epoch()
            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as state_spy:
                self.assertTrue(self._request_identity(obj, "CODEC"))

                # Detach/retire happens synchronously, while NULL teardown
                # is an in-flight bounded worker operation.
                self.assertIsNone(slot.current_bin)
                self.assertIsNone(slot.current_sink)
                self.assertIsNone(slot.pending_bin)
                self.assertEqual(slot.coordinator.generation, 1)
                self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.RECOVERING)
                self.assertEqual(slot.coordinator.snapshot()["operation_state"], "IN_FLIGHT")
                self.assertEqual(slot.device_loss_epoch(), epoch_before + 1)
                self.assertFalse(obj._output_slot_owns_message_src(
                    slot, type("M", (), {"src": old_child})()))

                complete_successful_retarget(obj, slot)

            state = obj._output_recovery_state()["studio_monitor"]
            self.assertEqual(slot.coordinator.generation, 2)
            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.OK)
            self.assertEqual(state["operation_state"], "RETURNED")
            self.assertIsNot(slot.current_bin, old_bin)
            self.assertIsNotNone(slot.current_sink)
            self.assertIsNone(slot.pending_bin)
            self.assertIsNone(slot.pending_sink)
            self.assertIsNone(slot.pending_validation_epoch)
            self.assertEqual((slot.identity_kind, slot.identity), ("alsa_card_id", "CODEC"))
            self.assertEqual(state["resolved_runtime_device"], "plughw:CARD=CODEC,DEV=0")
            self.assertEqual(state["active_device"], "plughw:CARD=CODEC,DEV=0")
            self.assertFalse(slot.retarget_requested)
            self.assertFalse(slot.retarget_in_progress)
            self.assertFalse(slot.valve.get_property("drop"))
            self.assertEqual(state_spy.call_args_list, [])

            codec_bin = slot.current_bin
            self.assertTrue(self._request_identity(obj, "PCH"))
            complete_successful_retarget(obj, slot)
            state = obj._output_recovery_state()["studio_monitor"]
            self.assertIsNot(slot.current_bin, codec_bin)
            self.assertEqual((slot.identity_kind, slot.identity), ("alsa_card_id", "PCH"))
            self.assertEqual(state["resolved_runtime_device"], "plughw:CARD=PCH,DEV=0")
            self.assertEqual(state["active_device"], "plughw:CARD=PCH,DEV=0")
            self.assertEqual(slot.coordinator.generation, 4)
            self.assertEqual(calls[-2:], ["plughw:CARD=CODEC,DEV=0", "plughw:CARD=PCH,DEV=0"])
        finally:
            pipeline.set_state(Gst.State.NULL)

    def _assert_failed_target(self, target):
        target_device = f"plughw:CARD={target},DEV=0"
        failing = {target_device}
        obj, pipeline, slot = self._make_slot(failing_devices=failing)
        try:
            old_bin = slot.current_bin
            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as state_spy, \
                 patch.object(eng_module, "OUTPUT_HEALTH_CHECK_DEADLINE_S", 0.2):
                self.assertTrue(self._request_identity(obj, target))
                self.assertTrue(wait_until(
                    lambda: slot.coordinator.state == audio_recovery.SlotState.OK))
                obj._output_handle_slot_transition(
                    slot, "RECOVERING", "OK", slot.coordinator.snapshot())
                self.assertTrue(wait_until(
                    lambda: slot.coordinator.state == audio_recovery.SlotState.DEGRADED,
                    timeout=2.0))

            self.assertIsNone(slot.current_bin)
            self.assertIsNone(slot.current_sink)
            self.assertIsNotNone(slot.pending_bin)
            self.assertTrue(slot.valve.get_property("drop"))
            self.assertEqual(slot.identity, target)
            self.assertEqual(state_spy.call_args_list, [])
            self.assertIsNot(slot.pending_bin, old_bin)

            # Failed candidate teardown is also bounded; after it resolves,
            # normal stable-identity recovery owns later retries.
            obj._output_handle_slot_transition(
                slot, "RECOVERING", "DEGRADED", slot.coordinator.snapshot())
            wait_for_pending_discard_to_resolve(obj, slot)
            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.DEGRADED)
            self.assertIsNone(slot.pending_bin)

            failing.remove(target_device)
            with patch.object(audio_recovery, "read_alsa_cards_present", lambda: {target: 5}):
                obj._output_presence_probe_tick()
            self.assertTrue(wait_until(
                lambda: slot.coordinator.state == audio_recovery.SlotState.OK))
            obj._output_handle_slot_transition(
                slot, "RECOVERING", "OK", slot.coordinator.snapshot())
            self.assertEqual(slot.current_sink.get_property("device"), target_device)
            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.OK)
            self.assertFalse(slot.valve.get_property("drop"))
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_missing_target_degrades_then_recovers_requested_identity(self):
        self._assert_failed_target("MISSING")

    def test_busy_open_failure_degrades_then_recovers_requested_identity(self):
        self._assert_failed_target("BUSY")

    def test_stale_error_from_detached_generation_is_ignored(self):
        obj, pipeline, slot = self._make_slot()
        try:
            old_bin = slot.current_bin
            old_child = old_bin.iterate_elements().next()[1]
            self.assertTrue(self._request_identity(obj, "CODEC"))
            generation = slot.coordinator.generation
            epoch = slot.device_loss_epoch()
            self.assertTrue(obj._on_main_bus_error(
                None, fake_error_message(
                    "Error outputting to audio device. The device has been disconnected.",
                    src=old_child)))
            self.assertEqual(slot.coordinator.generation, generation)
            self.assertEqual(slot.device_loss_epoch(), epoch)
            complete_successful_retarget(obj, slot)
            self.assertEqual(slot.current_sink.get_property("device"), "plughw:CARD=CODEC,DEV=0")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_old_sink_null_transition_runs_on_bounded_worker_thread(self):
        obj, pipeline, slot = self._make_slot()
        old_bin = slot.current_bin
        caller = threading.current_thread()
        threads = []
        original = old_bin.set_state

        def recording_set_state(state):
            threads.append(threading.current_thread())
            return original(state)

        try:
            with patch.object(old_bin, "set_state", side_effect=recording_set_state):
                self.assertTrue(self._request_identity(obj, "CODEC"))
                complete_successful_retarget(obj, slot)
            self.assertTrue(threads)
            self.assertTrue(all(thread is not caller for thread in threads))
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_failed_target_keeps_stereotool_playing_and_continuous(self):
        obj = make_output_engine_stand_in()
        pipeline = Gst.Pipeline.new("failed-retarget-sibling-harness")
        obj.main_pipeline = pipeline
        failed_device = "plughw:CARD=MISSING,DEV=0"
        monitor_builder = make_device_aware_generation_builder({failed_device})
        sibling_builder = make_synthetic_generation_builder()
        with patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                          lambda self, name: (("alsa_card_id", "PCH")
                                             if name == "Studio Monitor" else ("", ""))):
            monitor = obj._build_output_slot(
                "Studio Monitor", "studio_monitor", "device-a", monitor_builder)
            stereotool = obj._build_output_slot(
                "Stereotool Input", "stereotool", "stereotool-device", sibling_builder)
        obj._studio_monitor_slot = monitor
        obj._stereotool_slot = stereotool
        obj._output_slots = {"studio_monitor": monitor, "stereotool": stereotool}
        obj.running = True

        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("is-live", True)
        tee = Gst.ElementFactory.make("tee", None)
        for element in (src, tee, monitor.queue, monitor.errorignore, monitor.valve,
                        monitor.current_bin, stereotool.queue, stereotool.errorignore,
                        stereotool.valve, stereotool.current_bin):
            pipeline.add(element)
        src.link(tee)
        for slot in (monitor, stereotool):
            tee.link(slot.queue)
            slot.queue.link(slot.errorignore)
            slot.errorignore.link(slot.valve)
            slot.valve.get_static_pad("src").link(slot.current_bin.get_static_pad("sink"))

        timestamps = []
        states = []
        def probe(pad, info):
            timestamps.append(time.monotonic())
            states.append((pipeline.get_state(0)[1], stereotool.current_bin.get_state(0)[1]))
            return Gst.PadProbeReturn.OK
        stereotool.current_sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)
        try:
            pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(wait_until(lambda: len(timestamps) >= 10))
            start = len(timestamps)
            with patch.object(pipeline, "set_state", wraps=pipeline.set_state) as state_spy, \
                 patch.object(eng_module, "OUTPUT_HEALTH_CHECK_DEADLINE_S", 0.2), \
                 patch.object(eng_module.PlaybackEngine, "_resolve_output_device_identity",
                              lambda self, name: (("alsa_card_id", "MISSING")
                                                 if name == "Studio Monitor" else ("", ""))):
                changes = obj._reload_output_recovery_identity()
                self.assertTrue(obj._apply_audio_output_device(
                    "device-a", identity_changed="studio_monitor" in changes))
                self.assertTrue(wait_until(
                    lambda: monitor.coordinator.state == audio_recovery.SlotState.OK))
                obj._output_handle_slot_transition(
                    monitor, "RECOVERING", "OK", monitor.coordinator.snapshot())
                self.assertTrue(wait_until(
                    lambda: monitor.coordinator.state == audio_recovery.SlotState.DEGRADED,
                    timeout=2.0))
                self.assertTrue(wait_until(lambda: len(timestamps) >= start + 10))

            self.assertEqual(state_spy.call_args_list, [])
            self.assertEqual(pipeline.get_state(0)[1], Gst.State.PLAYING)
            self.assertEqual(stereotool.current_bin.get_state(0)[1], Gst.State.PLAYING)
            self.assertTrue(all(a == Gst.State.PLAYING and b == Gst.State.PLAYING
                                for a, b in states[start:]))
            intervals = [b - a for a, b in zip(timestamps, timestamps[1:])]
            max_gap = max(intervals)
            print(f"  StereoTool failed-retarget max inter-buffer gap: {max_gap:.6f}s")
            self.assertLess(max_gap, 0.25)
            self.assertTrue(monitor.valve.get_property("drop"))
            self.assertIsNone(monitor.current_bin)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_identity_retarget_racing_pending_recovery_discards_old_candidate(self):
        calls = []
        obj, pipeline, slot = self._make_slot(calls=calls)
        try:
            simulate_device_loss_and_quiesce(obj, slot)
            with patch.object(eng_module, "OUTPUT_HEALTH_STABILIZATION_S", 0.4):
                obj._output_dispatch_rebuild(slot)  # candidate for PCH
                old_pending = slot.pending_bin
                self.assertTrue(self._request_identity(obj, "CODEC"))
                self.assertTrue(wait_until(
                    lambda: slot.coordinator.state == audio_recovery.SlotState.DEGRADED))

            # Epoch invalidation makes the old-identity candidate fail;
            # discard it through the normal bounded pending-bin path.
            obj._output_handle_slot_transition(
                slot, "RECOVERING", "DEGRADED", slot.coordinator.snapshot())
            self.assertIsNone(slot.current_bin)
            self.assertIsNone(slot.pending_bin)
            self.assertIsNot(old_pending, slot.current_bin)
            # The discard's completion observes retarget_requested and
            # dispatches the replacement for the new identity.
            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK))
            obj._output_handle_slot_transition(
                slot, "RECOVERING", "OK", slot.coordinator.snapshot())
            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK))
            obj._output_handle_slot_transition(
                slot, "RECOVERING", "OK", slot.coordinator.snapshot())
            self.assertEqual(slot.current_sink.get_property("device"), "plughw:CARD=CODEC,DEV=0")
            self.assertEqual(calls[-1], "plughw:CARD=CODEC,DEV=0")
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


_DEVICE_LOST_ERROR_TEXT = ("gst-resource-error-quark: Error outputting to audio device. "
                            "The device has been disconnected. (10)")


class OutputStaleGenerationOwnershipTests(MockEmitEventMixin, SimpleTestCase):
    """[P0] 1.3C physical-acceptance-failure fix -- Bug 1. A real UCA222
    unplug/replug reproduced a genuine production event storm: ~560
    "Studio Monitor output lost" events in ~2.5s off ONE physical
    unplug, each with its OWN generation-specific dedupe key, driven by
    SlotCoordinator.generation climbing without bound. Root cause:
    _output_quiesce_current_generation detached (unlinked+removed) the
    old generation but never retired slot.current_bin/current_sink --
    so _output_slot_owns_message_src (which reads them fresh on every
    call, exactly to avoid this) kept matching the remaining ALSA error
    burst from that already-detached generation against the slot, long
    after it stopped being current. Every one of those stale matches
    ran through _on_output_error again -- and once the coordinator had
    cycled back to OK (which a real teardown can do in milliseconds,
    the other half of this fix -- see OutputFastOperationObserverTests),
    mark_degraded() legitimately succeeded AGAIN, off a message that
    was never really new.

    These tests route messages through the REAL _on_main_bus_error/
    _output_slot_owns_message_src path (not the _on_output_error
    shortcut most other tests in this file use) specifically because
    that ownership routing is what's under test here."""

    def test_stale_current_generation_error_ignored_after_fast_worker_resolves(self):
        """The realistic fast-error-burst reproduction (required test
        1): drives the REAL, undelayed quiesce worker to completion
        BEFORE sending the rest of the burst -- the single most
        adversarial ordering (the exact condition that let the real
        storm happen), rather than artificially slowing the worker down
        (which is why an earlier 62-message test passed for the wrong
        reason -- it never let the worker actually finish first).
        Proves: exactly one genuine loss episode, exactly one quiesce,
        no generation runaway, all stale detached-generation errors
        ignored, one coalesced SystemEvent, valve stays closed."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            stale_src = slot.current_sink  # captured BEFORE quiesce retires slot.current_sink
            msg1 = fake_error_message(_DEVICE_LOST_ERROR_TEXT, src=stale_src)

            with patch.object(slot.coordinator, "request_recovery",
                               wraps=slot.coordinator.request_recovery) as dispatch_spy:
                self.assertTrue(obj._on_main_bus_error(None, msg1))
                # The real bounded teardown can return before this thread
                # regains the GIL; both states preserve the ownership
                # contract this test exercises.
                self.assertIn(
                    slot.coordinator.state,
                    (audio_recovery.SlotState.RECOVERING,
                     audio_recovery.SlotState.OK),
                )
                self.assertEqual(dispatch_spy.call_count, 1)
                # The whole point of the fix: retirement is synchronous,
                # not dependent on the worker having finished yet.
                self.assertIsNone(slot.current_bin, "current generation must be retired immediately")
                self.assertIsNone(slot.current_sink)
                self.assertEqual(len(self.emitted_events), 1)
                self.assertEqual(slot.coordinator.generation, 1)

                # Let the REAL background worker actually finish -- no
                # artificial delay anywhere in this test.
                self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK,
                                            timeout=3.0),
                                 "the real quiesce worker did not resolve in time")

                gen_checkpoint = slot.coordinator.generation
                events_checkpoint = list(self.emitted_events)
                dispatch_checkpoint = dispatch_spy.call_count

                # The rest of the burst: same stale src, tight loop, zero delay.
                for _ in range(60):
                    obj._on_main_bus_error(None, fake_error_message(_DEVICE_LOST_ERROR_TEXT, src=stale_src))

                self.assertEqual(slot.coordinator.generation, gen_checkpoint,
                                  "no generation runaway from the stale burst")
                self.assertEqual(self.emitted_events, events_checkpoint,
                                  "no additional SystemEvent from the stale burst -- exactly one loss episode")
                self.assertEqual(dispatch_spy.call_count, dispatch_checkpoint,
                                  "no new worker dispatched for any stale burst message")
                self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.OK,
                                  "coordinator must not be re-degraded by stale messages")
                self.assertEqual(slot.valve.get_property("drop"), True,
                                  "valve stays closed after a quiesce-only success (only a "
                                  "PROMOTED rebuild reopens it) -- stale burst messages must not "
                                  "disturb that either way")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_stale_current_generation_error_explicitly_not_owned(self):
        """Focused, minimal version of required test 3: after the
        current generation is detached, a bus error whose source is
        that old bin/sink is unambiguously not owned by the slot."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            old_bin, old_sink = slot.current_bin, slot.current_sink
            obj._on_output_error(slot, None, fake_error_message(_DEVICE_LOST_ERROR_TEXT))
            self.assertIsNone(slot.current_bin)
            self.assertIsNone(slot.current_sink)

            stale_msg = fake_error_message(_DEVICE_LOST_ERROR_TEXT, src=old_sink)
            self.assertFalse(obj._output_slot_owns_message_src(slot, stale_msg))
            stale_msg_bin = fake_error_message(_DEVICE_LOST_ERROR_TEXT, src=old_bin)
            self.assertFalse(obj._output_slot_owns_message_src(slot, stale_msg_bin))

            # Cleanup hygiene: old_bin is already detached from
            # main_pipeline (that's the whole point of this test), so
            # pipeline.set_state(NULL) below can't reach it -- let the
            # quiesce's own background worker finish first.
            wait_until(lambda: slot.coordinator.state != audio_recovery.SlotState.RECOVERING, timeout=3.0)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_stale_pending_generation_error_ignored_after_discard(self):
        """Required test 4. _output_discard_pending_bin already clears
        slot.pending_bin/pending_sink at the very top, before any detach
        or worker dispatch -- confirmed by direct code reading, and
        locked in here: a bus error whose source is a just-discarded
        candidate must not be attributed to the slot, must not
        mark_degraded, must not bump generation, must not emit an event,
        must not dispatch a new worker.

        Uses the same guaranteed-fail "build_disconnected" generation as
        test_rebuild_that_never_renders_fails_and_recloses_valve, and
        waits for the REAL rebuild worker to actually finish (DEGRADED)
        BEFORE discarding -- calling discard immediately after dispatch,
        while the rebuild's own worker is still in flight, would just
        coalesce onto it (request_recovery() correctly refuses a second
        concurrent operation), never actually running the discard's own
        NULL-teardown worker at all, which both defeats what this test
        is trying to exercise and leaves the candidate bin's teardown
        with nothing to ever drive it to NULL."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            simulate_device_loss_and_quiesce(obj, slot)  # -> DEGRADED, no pending bin

            def build_disconnected(device):
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
                return bin_, sink

            slot.build_generation_fn = build_disconnected
            obj._output_dispatch_rebuild(slot)
            self.assertIsNotNone(slot.pending_bin)
            candidate_sink = slot.pending_sink

            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.DEGRADED,
                                        timeout=OUTPUT_TEST_TIMEOUT),
                             "the rebuild worker must genuinely finish (fail) before discarding")

            with patch.object(slot.coordinator, "request_recovery",
                               wraps=slot.coordinator.request_recovery) as dispatch_spy:
                obj._output_discard_pending_bin(slot, abandoned=False)
                self.assertIsNone(slot.pending_bin)
                self.assertIsNone(slot.pending_sink)

                gen_checkpoint = slot.coordinator.generation
                events_checkpoint = list(self.emitted_events)
                dispatch_checkpoint = dispatch_spy.call_count

                stale_msg = fake_error_message(_DEVICE_LOST_ERROR_TEXT, src=candidate_sink)
                self.assertFalse(obj._output_slot_owns_message_src(slot, stale_msg))
                result = obj._on_main_bus_error(None, stale_msg)
                self.assertTrue(result)  # routed to nothing, handler still returns True (no-op)

                self.assertEqual(slot.coordinator.generation, gen_checkpoint, "no generation increment")
                self.assertEqual(self.emitted_events, events_checkpoint, "no event")
                self.assertEqual(dispatch_spy.call_count, dispatch_checkpoint, "no new worker")

            # Cleanup hygiene: the discarded candidate is already
            # detached from main_pipeline, so pipeline.set_state(NULL)
            # below can't reach it -- let its own background worker
            # finish tearing it down first (same rationale as
            # wait_for_pending_discard_to_resolve elsewhere in this file).
            wait_until(lambda: slot.coordinator.state != audio_recovery.SlotState.RECOVERING, timeout=3.0)
        finally:
            pipeline.set_state(Gst.State.NULL)


class OutputFastOperationObserverTests(MockEmitEventMixin, SimpleTestCase):
    """[P0] 1.3C physical-acceptance-failure fix -- Bug 1, item 4
    (required test 2). _output_recovery_tick only calls
    _output_handle_slot_transition when snapshot["state"] differs from
    slot.last_observed_slot_state, polled every ~300ms. A real UCA222
    teardown can complete in well under that -- OK->DEGRADED->
    RECOVERING->OK entirely between two ticks -- and pre-fix,
    last_observed_slot_state was never updated at dispatch time, only by
    the tick itself, so a tick landing after such a fast round-trip saw
    new_state == old_state == "OK" and silently skipped the transition
    handler: no re-degrade, the slot stranded reporting phantom "OK"
    with a torn-down generation, no automatic rebuild ever attempted.

    Fixed via _output_request_recovery, which records RECOVERING into
    last_observed_slot_state synchronously the moment request_recovery()
    returns "dispatched" (a guaranteed-true fact at that exact instant,
    per SlotCoordinator's own locking -- see that method's docstring).

    Deliberately does NOT manually invoke _output_handle_slot_transition
    anywhere in this test -- that would hide exactly the race being
    proven here. Only the real dispatch path and the real tick are used."""

    def test_missed_intermediate_state_still_processed_by_next_real_tick(self):
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            obj.running = True
            obj._on_output_error(slot, None, fake_error_message(_DEVICE_LOST_ERROR_TEXT))
            self.assertEqual(slot.last_observed_slot_state, audio_recovery.SlotState.RECOVERING.value,
                              "the fix: dispatch must synchronously record RECOVERING as observed")

            # Force the exact production race: the real worker resolves
            # ALL THE WAY back to OK before _output_recovery_tick gets
            # even one chance to run.
            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK, timeout=3.0))
            self.assertIsNone(slot.pending_bin)  # this was a quiesce, not a rebuild

            obj._output_recovery_tick()  # the REAL tick -- no manual transition-handler call

            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.DEGRADED,
                              "the quiesce completion must still be processed: re-degraded, "
                              "ready for presence-probing to eventually dispatch a rebuild")
            self.assertEqual(slot.last_observed_slot_state, audio_recovery.SlotState.DEGRADED.value)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_candidate_rebuild_dispatch_marks_recovering_immediately(self):
        """Audits the second of the three named dispatch sites: candidate
        rebuild (quiesce is covered by the test above)."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            simulate_device_loss_and_quiesce(obj, slot)  # -> DEGRADED, ready for a rebuild dispatch
            slot.last_observed_slot_state = audio_recovery.SlotState.DEGRADED.value
            obj._output_dispatch_rebuild(slot)
            self.assertEqual(slot.last_observed_slot_state, audio_recovery.SlotState.RECOVERING.value,
                              "candidate rebuild dispatch must mark RECOVERING immediately")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_discard_pending_dispatch_site_and_tick_bookkeeping_survive_nested_dispatch(self):
        """Audits the third dispatch site (pending-generation discard)
        AND a second, related bug this review's fix surfaced: discard-
        pending is itself invoked FROM WITHIN _output_handle_slot_
        transition's DEGRADED branch (a failed rebuild is discarded
        immediately) -- so its own request_recovery() call, and this
        method's own last_observed_slot_state update, both happen
        NESTED INSIDE a call the tick itself is in the middle of. The
        tick's own post-handler bookkeeping must not blindly stomp that
        nested update back down to the pre-handler snapshot value (see
        _output_recovery_tick's own docstring) -- proven here by driving
        the WHOLE sequence through real, undelayed dispatch and real
        ticks only, never manually invoking _output_handle_slot_
        transition."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            simulate_device_loss_and_quiesce(obj, slot)  # -> DEGRADED, no pending bin

            def build_disconnected(device):
                # Same "reaches PLAYING but can never render" technique
                # as test_rebuild_that_never_renders_fails_and_recloses_
                # valve -- guarantees this candidate's health
                # verification genuinely fails, deterministically.
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
                return bin_, sink

            slot.build_generation_fn = build_disconnected
            obj._output_dispatch_rebuild(slot)
            self.assertEqual(slot.last_observed_slot_state, audio_recovery.SlotState.RECOVERING.value)

            # Real, undelayed worker resolving the (guaranteed) failure --
            # no artificial slowdown anywhere.
            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.DEGRADED,
                                        timeout=OUTPUT_TEST_TIMEOUT))
            self.assertIsNotNone(slot.pending_bin, "the failed candidate must still be in place pre-tick")

            obj.running = True
            with patch.object(slot.coordinator, "request_recovery",
                               wraps=slot.coordinator.request_recovery) as dispatch_spy:
                obj._output_recovery_tick()  # the REAL tick -- drives discard-pending internally

            self.assertEqual(dispatch_spy.call_count, 1,
                              "the tick's own call into the transition handler must have dispatched "
                              "the discard-pending NULL worker exactly once")
            self.assertIsNone(slot.pending_bin, "failed candidate must be discarded")
            # The crux: after the nested discard-pending dispatch,
            # last_observed_slot_state must reflect THAT fresh dispatch
            # (RECOVERING), not the pre-handler DEGRADED snapshot the
            # tick captured before calling the handler. Under real,
            # uncontrolled timing (this test doesn't force the ordering
            # either way), this is safe REGARDLESS of how fast the
            # discard's own background worker resolves, specifically
            # because of the recovery_dispatch_serial guard in
            # _output_recovery_tick (review round 2) -- an EARLIER
            # version of this fix (round 1: unconditionally re-reading
            # coordinator.state fresh after the handler returns) was
            # ITSELF still racy here: if that worker resolves to OK
            # before the handler returns, "re-read fresh" would see OK
            # and erase this marker. See
            # test_nested_dispatch_fast_completion_before_handler_return_
            # is_not_lost below for the version of this exact scenario
            # that FORCES the worker to resolve before the handler
            # returns, rather than leaving it to chance.
            self.assertEqual(slot.last_observed_slot_state, audio_recovery.SlotState.RECOVERING.value,
                              "tick bookkeeping must not stomp the nested dispatch's own update")

            # Cleanup hygiene only, no bearing on the assertions above:
            # the nested discard already detached its bin from
            # main_pipeline (see _output_discard_pending_bin), so
            # pipeline.set_state(NULL) below can no longer reach it --
            # its own background worker owns tearing IT down. Let that
            # finish before the pipeline teardown/test end, same
            # rationale as wait_for_pending_discard_to_resolve elsewhere
            # in this file (avoids a bin still in PLAYING racing
            # Python's GC against its own daemon teardown thread).
            wait_until(lambda: slot.coordinator.state != audio_recovery.SlotState.RECOVERING, timeout=3.0)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_nested_dispatch_fast_completion_before_handler_return_is_not_lost(self):
        """[P0] 1.3C physical-acceptance-failure fix, review round 2 --
        the exact race flagged: a nested operation dispatched from
        INSIDE _output_handle_slot_transition (here, discard-pending)
        resolves all the way back to OK BEFORE the handler call itself
        returns. Round 1's fix ("re-read coordinator.state fresh after
        the handler returns") was still racy against exactly this
        ordering -- a value comparison can't tell "nothing new was
        dispatched" apart from "something new was dispatched and
        already finished". Fixed with slot.recovery_dispatch_serial.

        FORCES the ordering deterministically -- never hopes scheduler
        timing cooperates. Wraps _output_request_recovery itself so that,
        the instant it reports "dispatched" for the NESTED call (the
        discard's own NULL-teardown worker), this test blocks the
        calling thread (the tick's own thread, still inside the handler)
        until the real, undelayed worker has ACTUALLY resolved the
        coordinator to OK -- guaranteeing the worker finishes strictly
        before _output_handle_slot_transition, and therefore this whole
        tick call, returns. The real tick and the real observer
        bookkeeping are otherwise untouched -- only the timing is forced,
        not the outcome."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            simulate_device_loss_and_quiesce(obj, slot)  # -> DEGRADED, no pending bin

            def build_disconnected(device):
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
                return bin_, sink

            slot.build_generation_fn = build_disconnected
            obj._output_dispatch_rebuild(slot)
            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.DEGRADED,
                                        timeout=OUTPUT_TEST_TIMEOUT),
                             "the rebuild worker must genuinely fail before the first tick runs")
            self.assertIsNotNone(slot.pending_bin)
            self.assertEqual(slot.last_observed_slot_state, audio_recovery.SlotState.RECOVERING.value,
                              "primes the mismatch the first tick must detect: last_observed still "
                              "RECOVERING (from the rebuild dispatch above) vs. the real DEGRADED state")

            real_request_recovery = obj._output_request_recovery

            def blocking_request_recovery(slot_arg, worker):
                result = real_request_recovery(slot_arg, worker)
                if result == "dispatched":
                    # Force the exact adversarial ordering: don't return
                    # control to the caller (here, _output_discard_pending_
                    # bin, and therefore _output_handle_slot_transition,
                    # and therefore this whole tick call) until the real
                    # background worker has ACTUALLY resolved this
                    # operation. No artificial worker slowdown anywhere --
                    # only this test's own return is delayed.
                    self.assertTrue(
                        wait_until(lambda: slot_arg.coordinator.state == audio_recovery.SlotState.OK, timeout=3.0),
                        "the nested discard's NULL-teardown worker must resolve to OK")
                return result

            obj.running = True
            with patch.object(obj, "_output_request_recovery", side_effect=blocking_request_recovery):
                obj._output_recovery_tick()  # first REAL tick -- drives the nested discard, forced to finish inline

            # After the first tick: the nested worker has DEFINITELY
            # already resolved to OK (forced above, not hoped for) --
            # but last_observed_slot_state must still read RECOVERING,
            # never OK, per the fix. This is the assertion round 1's fix
            # could not reliably satisfy.
            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.OK,
                              "sanity: the nested worker really did finish before this point")
            self.assertEqual(slot.last_observed_slot_state, audio_recovery.SlotState.RECOVERING.value,
                              "the RECOVERING marker must survive the handler returning, even though "
                              "the nested operation it dispatched already resolved to OK")
            self.assertIsNone(slot.pending_bin, "the failed candidate was still correctly discarded")

            # Second REAL tick -- must recognize the now-stale
            # RECOVERING -> OK transition and actually process it
            # (re-degrade after what was, functionally, a successful
            # quiesce-equivalent teardown), landing the slot in the
            # correct DEGRADED/waiting-for-presence state -- not stuck
            # reporting a stale, already-superseded observation forever.
            obj._output_recovery_tick()
            self.assertEqual(slot.coordinator.state, audio_recovery.SlotState.DEGRADED,
                              "second tick must process the missed completion and re-degrade")
            self.assertEqual(slot.last_observed_slot_state, audio_recovery.SlotState.DEGRADED.value)
        finally:
            pipeline.set_state(Gst.State.NULL)


def _build_generation_with_sink_props(async_val, sync_val):
    """Test-only generation builder -- same shape as
    _build_studio_monitor_hw_generation, with async/sync EXPLICITLY
    controlled (rather than that method's own choice baked in) so a
    test can directly compare the pre-fix (async=True, GstBaseSink's
    own default) and post-fix (async=False) Studio Monitor behavior
    through the REAL recovery choreography, without touching real ALSA
    hardware. fakesink is a legitimate stand-in specifically for THIS
    experiment: async/sync preroll-gating is implemented in GstBaseSink
    itself (a common base class alsasink inherits unmodified), not
    sink-type-specific -- confirmed directly via the standalone harness
    in scratchpad/audio_output_recovery/ before this fix was written."""
    def build(device):
        bin_ = Gst.Bin.new(f"preroll_test_gen{int(time.time() * 1_000_000)}")
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("async", async_val)
        sink.set_property("sync", sync_val)
        bin_.add(sink)
        ghost = Gst.GhostPad.new("sink", sink.get_static_pad("sink"))
        ghost.set_active(True)
        bin_.add_pad(ghost)
        return bin_, sink
    return build


class OutputStudioMonitorPrerollRegressionTests(MockEmitEventMixin, SimpleTestCase):
    """[P0] 1.3C physical-acceptance-failure fix -- Bug 2 (required test
    5). Root cause: _output_dispatch_rebuild links a fresh candidate
    behind a CLOSED valve and waits for it to reach PLAYING before ever
    opening that valve. async=True (GstBaseSink's own default, and what
    _build_studio_monitor_hw_generation left Studio Monitor's alsasink
    at, pre-fix) makes PAUSED->PLAYING itself asynchronous, gated on
    receiving a first buffer to preroll with -- which a closed valve
    upstream can never deliver. Circular: needs a buffer to reach
    PLAYING, needs PLAYING before the valve opens, valve closed means no
    buffer. Confirmed by physical UCA222 testing (every real rebuild
    timed out at ~6s and failed health verification, repeated every
    retry -- no automatic recovery, audio only returned after an
    operator restarted the engine) and by the standalone empirical
    harness this test's first method reproduces directly."""

    def test_async_true_cannot_reach_playing_behind_closed_valve(self):
        """Raw GstBaseSink reproduction of the empirical preroll-gate
        harness (scratchpad/audio_output_recovery/) -- audiotestsrc ->
        valve(drop=True) -> fakesink(async=True). Demonstrates the OLD,
        broken Studio Monitor behavior directly, with no engine
        machinery involved at all."""
        pipeline = Gst.Pipeline.new("preroll-regression-async-true")
        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("is-live", True)
        valve = Gst.ElementFactory.make("valve", None)
        valve.set_property("drop", True)
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("async", True)
        sink.set_property("sync", True)
        for el in (src, valve, sink):
            pipeline.add(el)
        src.link(valve)
        valve.link(sink)
        try:
            pipeline.set_state(Gst.State.PLAYING)
            reached_playing = wait_until(
                lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING, timeout=1.5)
            self.assertFalse(reached_playing,
                              "async=True must NOT reach PLAYING behind a closed valve")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_async_false_reaches_playing_and_renders_after_valve_opens(self):
        """Same shape, async=False/sync=True (the fix) -- reaches
        PLAYING despite the closed valve, and opening the valve
        afterward makes the rendered count increase."""
        pipeline = Gst.Pipeline.new("preroll-regression-async-false")
        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("is-live", True)
        valve = Gst.ElementFactory.make("valve", None)
        valve.set_property("drop", True)
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("async", False)
        sink.set_property("sync", True)
        for el in (src, valve, sink):
            pipeline.add(el)
        src.link(valve)
        valve.link(sink)
        try:
            pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING, timeout=1.5),
                             "async=False must reach PLAYING even behind a closed valve")

            def rendered():
                stats = sink.get_property("stats")
                ok, val = stats.get_uint64("rendered")
                return val if ok else None

            self.assertEqual(rendered(), 0, "nothing should have rendered yet -- valve still closed")
            valve.set_property("drop", False)
            self.assertTrue(wait_until(lambda: (rendered() or 0) > 0, timeout=1.5),
                             "rendered count must increase once the valve opens")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_real_dispatch_rebuild_fails_with_async_true_studio_monitor_shape(self):
        """The SAME failure through the REAL _output_dispatch_rebuild
        choreography (not just the raw GstBaseSink harness) -- proves
        the fix matters to the actual recovery machinery, not only in
        the abstract. Deadline shrunk via patching so this stays a fast
        test; the mechanism under test (the circular PLAYING/valve gate)
        doesn't depend on the deadline's magnitude, only that it's
        eventually reached."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(
            obj, build_generation_fn=_build_generation_with_sink_props(async_val=True, sync_val=True))
        try:
            simulate_device_loss_and_quiesce(obj, slot)  # -> DEGRADED, ready for a rebuild dispatch
            with patch.object(eng_module, "OUTPUT_HEALTH_CHECK_DEADLINE_S", 0.4):
                obj._output_dispatch_rebuild(slot)
                self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.DEGRADED,
                                            timeout=3.0),
                                 "the pre-fix shape must fail health verification (never reaches PLAYING)")
            snapshot = slot.coordinator.snapshot()
            self.assertIs(snapshot["operation_succeeded"], False)
            self.assertEqual(slot.valve.get_property("drop"), True, "valve must stay closed on failure")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_real_dispatch_rebuild_succeeds_with_studio_monitor_fixed_shape(self):
        """The SAME choreography, async=False/sync=False (matching
        _build_studio_monitor_hw_generation's actual chosen properties
        post-fix) -- candidate reaches PLAYING behind the closed valve,
        the worker opens the valve, rendered count increases, and the
        rebuild is reported successful end to end."""
        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(
            obj, build_generation_fn=_build_generation_with_sink_props(async_val=False, sync_val=False))
        try:
            simulate_device_loss_and_quiesce(obj, slot)  # -> DEGRADED, ready for a rebuild dispatch
            obj._output_dispatch_rebuild(slot)
            self.assertTrue(wait_until(lambda: slot.coordinator.state == audio_recovery.SlotState.OK,
                                        timeout=OUTPUT_TEST_TIMEOUT),
                             "the fixed shape must pass health verification and succeed")
            snapshot = slot.coordinator.snapshot()
            self.assertIs(snapshot["operation_succeeded"], True)
            self.assertEqual(slot.valve.get_property("drop"), False, "valve must be open after success")
            rendered_ok, rendered_val = slot.pending_sink.get_property("stats").get_uint64("rendered")
            self.assertTrue(rendered_ok)
            self.assertGreater(rendered_val, 0, "rendered count must have genuinely increased")
        finally:
            pipeline.set_state(Gst.State.NULL)


class OutputMonitoringDedupeDefenseInDepthTests(TestCase):
    """[P0] 1.3C physical-acceptance-failure fix -- required test 8,
    engine-integration half (see monitoring/tests/test_emit_event_
    dedupe.py for the direct, thorough emit_event-level coverage --
    including a negative control proving a generation-suffixed key
    genuinely defeats coalescing, so THAT file's positive result isn't
    trivially true). This file closes the loop the other direction:
    proves the REAL _on_output_error call site, against the REAL
    database (no MockEmitEventMixin), actually uses the stable key
    format monitoring/tests/test_emit_event_dedupe.py already proved
    coalesces correctly -- i.e. that the fix in engine.py and the
    defense-in-depth in emit_event are actually wired together, not
    just each independently correct in isolation."""

    def test_first_failure_notification_uses_the_stable_dedupe_key(self):
        from monitoring.models import SystemEvent

        obj = make_output_engine_stand_in()
        pipeline, slot = build_slot_in_pipeline(obj)
        try:
            obj._on_output_error(slot, None, fake_error_message(_DEVICE_LOST_ERROR_TEXT))

            rows = list(SystemEvent.objects.filter(category="hardware", title="Studio Monitor output lost"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].dedupe_key, "hardware|output-lost|studio_monitor",
                              "must be the stable per-slot key, no coordinator.generation suffix -- "
                              "see monitoring/tests/test_emit_event_dedupe.py for proof THIS exact "
                              "key format coalesces hundreds of rapid repeats into one row")
            self.assertIn("generation", rows[0].detail)
            self.assertIn("loss_episode", rows[0].detail)

            self.assertTrue(wait_until(lambda: slot.coordinator.state != audio_recovery.SlotState.RECOVERING,
                                        timeout=3.0))
        finally:
            pipeline.set_state(Gst.State.NULL)
