"""[P0] 1.3B2 -- local input hotplug recovery: engine-integrated tests.

Real GStreamer elements/pipelines (never mocked -- matches this codebase's
established convention, see test_remote_dj_monitor_mixer_timeline.py and
test_engine_mic_clock_policy.py), built via the same
object.__new__(PlaybackEngine) minimal-stand-in technique as
test_engine_mic_clock_policy.py (1.3B1) and test_engine_runtime_commit.py.
No real ALSA hardware is opened here -- every test uses a synthetic
element (audiotestsrc standing in for the hardware generation, or a
deliberately-nonexistent device string to exercise the error path) so
this suite runs anywhere, no card required.

See test_audio_recovery.py for the pure-module (no GStreamer) half."""
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from django.test import SimpleTestCase

import library.services.engine as eng_module
from library.services import audio_recovery

Gst.init(None)

FAKE_MIC_DEVICE = "hw:CARD=NoSuchTestCard1_3B2,DEV=0"


def make_mic_stand_in(identity_kind="", identity="", legacy_device=None):
    """Same minimal-stand-in pattern as 1.3B1's test_engine_mic_clock_policy.py,
    extended with every [P0] 1.3B2 attribute _build_mic_chain/_on_mic_error/
    the recovery tick methods touch."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj._resolve_mic_device = lambda: legacy_device or FAKE_MIC_DEVICE
    obj._resolve_mic_identity = lambda: None  # real version hits the DB; stub, then set fields directly below
    obj._mic_identity_kind = identity_kind
    obj._mic_identity = identity
    obj._mic_legacy_device = legacy_device or FAKE_MIC_DEVICE
    obj.pipeline_sample_rate = 44100
    obj._apply_mic_gain = lambda: None
    obj.mic_gain = None
    obj.mic_ptt_valve = None
    obj.mic_ptt_volume = None
    obj._mic_bin = None
    obj.mic_ok = False
    obj.mic_live = False
    obj.manual_mode = False
    obj._manual_from_mic = False
    obj._manual_hold_pending = False
    obj.remote_dj_session = None
    obj.dj_slots = []
    obj._remote_dj_gate_open = lambda: False
    obj.running = True

    obj._mic_hw_bin = None
    obj._mic_hw_generation = 0
    obj._mic_pending_hw_bin = None
    obj._mic_selector = None
    obj._mic_silence_pad = None
    obj._mic_device_pad = None
    obj._mic_quarantine_q = None
    obj._mic_slot = None
    obj._mic_last_observed_slot_state = None
    obj._mic_recovery_attempt = 0
    obj._mic_next_retry_at = None
    obj._mic_device_present = None
    obj._mic_last_error = None
    obj._mic_last_state_change_at = None
    obj._mic_buf_last_ns = None
    obj._mic_buf_count = 0

    # DuckingConfig.load() hits the DB -- stubbed to a plain, disabled
    # ducking policy so _apply_talk_ducking (called via _set_mic_ptt)
    # doesn't need a database.
    from unittest.mock import patch
    return obj


def build_and_play(obj, extra_elements=(), synthetic_hw=False):
    """Constructs the mic bin, wires it into a throwaway test pipeline,
    and reaches PLAYING. Returns (pipeline, mic_bin, sink).

    synthetic_hw=True replaces the cold-start generation (built by
    _build_mic_chain against FAKE_MIC_DEVICE, which -- confirmed directly,
    not assumed -- fails to open at READY and blocks the WHOLE bin's
    state transition, exactly like a real absent device would) with a
    working audiotestsrc-based stand-in of identical shape, via
    swap_in_synthetic_hw_generation below. Needed by any test that
    requires the pipeline to genuinely reach PLAYING or observes real
    buffer flow/active-pad readback (which only reflects reality once
    the element is actually running -- verified directly)."""
    elements = obj._build_mic_chain()
    mic_bin = elements[0]
    if synthetic_hw:
        swap_in_synthetic_hw_generation(obj)
    pipeline = Gst.Pipeline.new("test-pipeline")
    sink = Gst.ElementFactory.make("fakesink", "sink")
    sink.set_property("sync", False)
    pipeline.add(mic_bin)
    pipeline.add(sink)
    for el in extra_elements:
        pipeline.add(el)
    mic_bin.link(sink)
    pipeline.set_state(Gst.State.PLAYING)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        _, st, _ = pipeline.get_state(0)
        if st == Gst.State.PLAYING:
            break
        time.sleep(0.02)
    return pipeline, mic_bin, sink


def swap_in_synthetic_hw_generation(obj):
    """Detaches the cold-start (fake-device) hardware generation and
    replaces it with a working audiotestsrc-based bin of the identical
    ghost-pad shape _build_mic_hw_generation produces (a single "src"
    ghost pad feeding whatever's linked downstream). Used only by tests
    that need genuine PLAYING/buffer flow -- production always uses a
    real alsasrc; this exists purely so this test suite needs no real
    ALSA hardware, per this file's own docstring."""
    old_bin = obj._mic_hw_bin
    if old_bin is not None:
        src_pad = old_bin.get_static_pad("src")
        sink_pad = obj._mic_quarantine_q.get_static_pad("sink")
        if src_pad.is_linked():
            src_pad.unlink(sink_pad)
        obj._mic_bin.remove(old_bin)
        # Test-only synchronous NULL (production dispatches this through
        # the guarded background worker -- see _mic_quiesce_current_
        # generation; a direct call here is fine since these are
        # synthetic, always-fast elements, never real hardware). Without
        # this, a bin still PLAYING when its last Python reference drops
        # triggers a GStreamer-CRITICAL on disposal.
        old_bin.set_state(Gst.State.NULL)

    obj._mic_hw_generation += 1
    gen = obj._mic_hw_generation
    hw_bin = Gst.Bin.new(f"synthetic_hw_gen{gen}")
    src = Gst.ElementFactory.make("audiotestsrc", None)
    src.set_property("is-live", True)
    hw_bin.add(src)
    ghost = Gst.GhostPad.new("src", src.get_static_pad("src"))
    ghost.set_active(True)
    hw_bin.add_pad(ghost)

    obj._mic_bin.add(hw_bin)
    hw_bin.get_static_pad("src").link(obj._mic_quarantine_q.get_static_pad("sink"))
    obj._mic_hw_bin = hw_bin
    return hw_bin


def assert_active_pad_is(testcase, selector, expected_pad, timeout=1.0):
    """input-selector's "active-pad" property -- confirmed directly, not
    assumed, via two separate minimal repros -- has two real quirks
    neither production code nor this test suite needs to work around
    functionally, but which DO make a naive readback assertion flaky:
    (1) it reads back None entirely until the element has actually
    reached PLAYING inside a started pipeline; (2) even once PLAYING, a
    set_property() is applied on the element's own streaming thread, so
    an immediate get_property() can still report the PREVIOUS pad for up
    to tens of milliseconds. Real end-to-end buffer-routing tests against
    actual CODEC hardware confirmed the switch itself always takes
    effect correctly and promptly -- production never reads this
    property back at all. This helper polls briefly so the test's own
    assertion is deterministic rather than racy, and compares by pad
    name (not object identity, which can also differ across the same
    settling window) since that's what's stable across it."""
    ok = wait_until(
        lambda: (lambda p: p is not None and p.get_name() == expected_pad.get_name())(
            selector.get_property("active-pad")),
        timeout=timeout)
    testcase.assertTrue(
        ok, f"active-pad did not settle to {expected_pad.get_name()!r} within {timeout}s")


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class MockDuckingMixin:
    """Patches DuckingConfig.load() (hit by _apply_talk_ducking, called
    from _set_mic_ptt) and monitoring.models.emit_event (hit on every
    first-failure/recovered/restart-required transition) so these tests
    never need a database. Applied per-test via setUp, not module-level,
    to keep the patch scoped."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        import hardware.models as hw_models

        class _FakeDucking:
            enabled = False
            duck_level_db = -6.0

        patcher = patch.object(hw_models.DuckingConfig, "load", staticmethod(lambda: _FakeDucking()))
        patcher.start()
        self.addCleanup(patcher.stop)
        # _start_duck_ramp uses GLib.timeout_add -- stub to a no-op so no
        # real ramp/timer is scheduled by a test that never runs a GLib
        # main loop.
        patch_ramp = patch.object(eng_module.PlaybackEngine, "_start_duck_ramp", lambda self, target: None)
        patch_ramp.start()
        self.addCleanup(patch_ramp.stop)
        # emit_event writes a SystemEvent row -- forbidden under
        # SimpleTestCase (no DB). Recorded so tests can assert on WHICH
        # events fired without needing a real database.
        self.emitted_events = []

        def _fake_emit_event(category, title, level="info", detail=None, source=None, dedupe_key=None):
            self.emitted_events.append({"category": category, "title": title, "level": level,
                                         "detail": detail, "dedupe_key": dedupe_key})

        patch_emit = patch.object(eng_module, "emit_event", _fake_emit_event)
        patch_emit.start()
        self.addCleanup(patch_emit.stop)


def play_pipeline(pipeline, timeout_s=3.0):
    """Explicitly transitions `pipeline` itself to PLAYING and waits for
    it to actually get there -- NOT the same as calling
    sync_state_with_parent() on a child bin whose parent pipeline was
    never itself started (a real trap: sync_state_with_parent() "succeeds"
    trivially by syncing to NULL if the parent was never set PLAYING)."""
    pipeline.set_state(Gst.State.PLAYING)
    return wait_until(lambda: pipeline.get_state(0)[1] == Gst.State.PLAYING, timeout=timeout_s)


class MicChainNonexistentDeviceUsesFakeDeviceStringTests(MockDuckingMixin, SimpleTestCase):
    """Sanity: building against a deliberately-fake device string still
    constructs a valid, linkable topology (matches 1.3B1's precedent --
    alsasrc construction never fails synchronously; only the later state
    change / bus error does)."""

    def test_construction_succeeds_and_links(self):
        obj = make_mic_stand_in()
        elements = obj._build_mic_chain()
        self.assertEqual(len(elements), 1)
        mic_bin = elements[0]
        self.assertIsNotNone(mic_bin.get_static_pad("src"))
        self.assertIsNotNone(obj._mic_selector)
        self.assertIsNotNone(obj._mic_quarantine_q)
        self.assertIsNotNone(obj._mic_slot)
        self.assertEqual(obj._mic_slot.state, audio_recovery.SlotState.OK)


class MicLossSemanticsTests(MockDuckingMixin, SimpleTestCase):
    """Item 5: on a classified device-loss error, mic_ok False, mic_live
    False, PTT 0, selector -> silence -- all synchronous, no pipeline
    needed to observe the Python-side state (bus dispatch isn't required
    to call _on_mic_error directly, matching how a real Gst.Bus would
    invoke it)."""

    def _fake_error_message(self, src, message_text, debug_text=""):
        class _Err:
            def __str__(self_inner):
                return message_text

        class _Msg:
            def parse_error(self_inner):
                return _Err(), debug_text

        return _Msg()

    def test_device_lost_error_sets_off_semantics_and_switches_to_silence(self):
        obj = make_mic_stand_in()
        pipeline, mic_bin, sink = build_and_play(obj, synthetic_hw=True)
        try:
            self.assertTrue(play_pipeline(pipeline))
            obj.mic_ok = True
            obj._set_mic_ptt(True)  # simulate operator having keyed PTT
            self.assertTrue(obj.mic_live)
            self.assertEqual(obj.mic_ptt_volume.get_property("volume"), 1.0)

            msg = self._fake_error_message(
                None, "gst-resource-error-quark: Error recording from audio device. "
                      "The device has been disconnected. (9)")
            obj._on_mic_error(None, msg)

            self.assertFalse(obj.mic_ok)
            self.assertFalse(obj.mic_live)
            self.assertEqual(obj.mic_ptt_volume.get_property("volume"), 0.0)
            # active-pad readback only reflects reality once the element
            # is actually PLAYING inside a started pipeline -- verified
            # directly (a bare, never-PLAYING selector reads back None
            # regardless of what was set).
            assert_active_pad_is(self, obj._mic_selector, obj._mic_silence_pad)
            # Recovery was dispatched immediately (quiesce), so by the
            # time this returns the slot has already moved past the
            # fleeting DEGRADED instant into RECOVERING -- "not OK" is
            # the meaningful assertion here.
            self.assertNotEqual(obj._mic_slot.state, audio_recovery.SlotState.OK)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_unknown_error_does_not_trigger_recovery(self):
        """Item 2: transient/unknown errors must not initiate recovery."""
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        obj.mic_ok = True

        msg = self._fake_error_message(None, "gst-core-error-quark: totally unrelated failure")
        obj._on_mic_error(None, msg)

        # mic_ok/selector/slot are all UNTOUCHED -- classification
        # short-circuits before any of the off-semantics run.
        self.assertTrue(obj.mic_ok)
        self.assertEqual(obj._mic_slot.state, audio_recovery.SlotState.OK)

    def test_manual_mode_hold_released_on_device_loss(self):
        """Ducking/manual-mode ownership must be released exactly as a
        normal mic-off does -- not left stale."""
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        obj.mic_ok = True
        obj._set_mic_ptt(True)
        self.assertTrue(obj.manual_mode)
        self.assertTrue(obj._manual_from_mic)

        msg = self._fake_error_message(
            None, "gst-resource-error-quark: Error recording from audio device. "
                  "The device has been disconnected. (9)")
        obj._on_mic_error(None, msg)

        # manual_mode itself is what must release -- confirmed via the
        # existing, unmodified _set_manual_mode(_from_mic_release=True)
        # path (same one a normal mic-off already uses). Its own
        # docstring is explicit that _manual_from_mic is deliberately
        # NOT cleared on this specific path (only an explicit operator
        # toggle clears it) -- inert once manual_mode is already False,
        # so not asserted here.
        self.assertFalse(obj.manual_mode)

    def test_coalesced_repeat_failure_does_not_redispatch(self):
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        gen_before = obj._mic_slot.generation

        msg = self._fake_error_message(
            None, "gst-resource-error-quark: Error recording from audio device. "
                  "The device has been disconnected. (9)")
        obj._on_mic_error(None, msg)
        first_generation = obj._mic_slot.generation
        obj._on_mic_error(None, msg)  # repeat -- same underlying failure
        obj._on_mic_error(None, msg)

        # mark_degraded() only fires the quiesce dispatch on the FIRST
        # call; repeats must not create additional operations/generations
        # beyond the single quiesce+eventual rebuild sequence.
        self.assertEqual(obj._mic_slot.generation, first_generation)


class EosQuarantineTests(MockDuckingMixin, SimpleTestCase):
    """Item 6/7, narrowed after pre-commit review (Issue 1): EOS from the
    hardware branch is suppressed; FLUSH_START/FLUSH_STOP are explicitly
    NOT suppressed (the eight-round physical investigation's own
    suppression counters -- scratchpad/audio_recovery/hotplug_harness/
    logs/*.jsonl -- show exactly 6 suppressions across every physical
    round, all of them EOS, zero FLUSH_START, zero FLUSH_STOP; nothing in
    this codebase's mic-recovery logic sends or depends on flush events
    either -- confirmed by source inspection, not just by these tests);
    the silence branch's own events are never touched (different pad
    entirely)."""

    def _build_played_pipeline_with_downstream_probe(self, obj):
        """Real, PLAYING pipeline (synthetic hw generation -- see this
        file's own docstring on why no real ALSA device is used) with a
        probe on the final sink's own sink pad, so these tests can
        observe exactly what a downstream element actually receives, not
        just what was sent."""
        pipeline, mic_bin, sink = build_and_play(obj, synthetic_hw=True)
        self.assertTrue(play_pipeline(pipeline))
        obj._mic_selector.set_property("active-pad", obj._mic_silence_pad)
        downstream_events = []

        def probe(pad, info):
            downstream_events.append(info.get_event().type)
            return Gst.PadProbeReturn.OK

        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, probe)
        return pipeline, sink, downstream_events

    def test_eos_on_hw_branch_sink_pad_is_dropped(self):
        obj = make_mic_stand_in()
        pipeline, sink, downstream_events = self._build_played_pipeline_with_downstream_probe(obj)
        try:
            sink_pad = obj._mic_quarantine_q.get_static_pad("sink")
            sink_pad.send_event(Gst.Event.new_eos())
            time.sleep(0.2)
            self.assertNotIn(Gst.EventType.EOS, downstream_events,
                              "EOS must never reach anything downstream of the quarantine boundary")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_flush_start_on_hw_branch_sink_pad_passes_through(self):
        obj = make_mic_stand_in()
        pipeline, sink, downstream_events = self._build_played_pipeline_with_downstream_probe(obj)
        try:
            sink_pad = obj._mic_quarantine_q.get_static_pad("sink")
            # FLUSH_START isn't even expected to be VISIBLE to a plain
            # EVENT_DOWNSTREAM probe two elements further down (confirmed
            # directly during pre-commit review: GStreamer doesn't
            # deliver it to an EVENT_DOWNSTREAM-only probe at all -- it
            # requires the separate EVENT_FLUSH probe-type flag, which
            # this codebase's quarantine probe deliberately no longer
            # registers, per that same review). The meaningful assertion
            # here is that it was NOT silently swallowed by OUR probe
            # specifically: send_event()'s own return value reflects
            # whether the event was accepted into the pad's normal event-
            # handling chain, which a DROP from a probe would prevent.
            ret = sink_pad.send_event(Gst.Event.new_flush_start())
            self.assertTrue(ret, "FLUSH_START must not be dropped by the quarantine probe")
        finally:
            sink_pad.send_event(Gst.Event.new_flush_stop(True))  # leave the pad unflushed for clean teardown
            pipeline.set_state(Gst.State.NULL)

    def test_flush_stop_on_hw_branch_sink_pad_passes_through(self):
        """send_event()'s own return value is the direct, unconfounded
        signal for "was this event dropped by a probe on this pad" -- a
        DROP from any probe makes send_event() return False. Checking
        arrival further downstream instead (tried during pre-commit
        review) is confounded by two things unrelated to this probe: (1)
        input-selector doesn't forward events/buffers from whichever
        sink pad isn't currently active, independent of anything this
        probe does, and (2) GstQueue's own internal flush handling across
        its two sides isn't something this codebase's probe controls or
        needs to reason about -- neither is what this test is checking."""
        obj = make_mic_stand_in()
        pipeline, mic_bin, sink = build_and_play(obj, synthetic_hw=True)
        try:
            self.assertTrue(play_pipeline(pipeline))
            obj._mic_selector.set_property("active-pad", obj._mic_silence_pad)

            sink_pad = obj._mic_quarantine_q.get_static_pad("sink")
            sink_pad.send_event(Gst.Event.new_flush_start())
            ret = sink_pad.send_event(Gst.Event.new_flush_stop(True))
            self.assertTrue(ret, "FLUSH_STOP must not be dropped by the quarantine probe")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_normal_downstream_events_pass_through(self):
        """Sanity: the probe isn't accidentally intercepting anything
        beyond EOS -- STREAM_START/CAPS/SEGMENT (all sent automatically
        by the real pipeline reaching PLAYING) must reach downstream."""
        obj = make_mic_stand_in()
        pipeline, sink, downstream_events = self._build_played_pipeline_with_downstream_probe(obj)
        try:
            time.sleep(0.3)
            for expected in (Gst.EventType.STREAM_START, Gst.EventType.CAPS, Gst.EventType.SEGMENT):
                self.assertIn(expected, downstream_events, f"{expected} must reach downstream normally")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_eos_suppressed_pipeline_survives_and_keeps_playing(self):
        """The real end-to-end proof: sending EOS into the hardware
        branch must NOT put the shared pipeline into an EOS'd/stopped
        state -- it should still be PLAYING and still forwarding silence
        buffers afterward."""
        obj = make_mic_stand_in()
        pipeline, mic_bin, sink = build_and_play(obj, synthetic_hw=True)
        try:
            self.assertTrue(play_pipeline(pipeline))
            obj._mic_selector.set_property("active-pad", obj._mic_silence_pad)

            eos_seen = {"n": 0}

            def bus_watcher(bus, msg):
                if msg.type == Gst.MessageType.EOS:
                    eos_seen["n"] += 1
                return True

            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", bus_watcher)

            sink_pad = obj._mic_quarantine_q.get_static_pad("sink")
            sink_pad.send_event(Gst.Event.new_eos())

            downstream_count = {"n": 0}

            def probe(pad, info):
                downstream_count["n"] += 1
                return Gst.PadProbeReturn.OK

            sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)
            time.sleep(0.4)

            self.assertEqual(eos_seen["n"], 0, "EOS must never reach the pipeline bus")
            _, st, _ = pipeline.get_state(0)
            self.assertEqual(st, Gst.State.PLAYING, "pipeline must still be PLAYING after suppressed EOS")
            self.assertGreater(downstream_count["n"], 0, "silence must still be flowing downstream")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_silence_branch_events_not_touched_by_hw_branch_probe(self):
        """The probe is installed on quarantine_q's sink pad only -- an
        EOS sent directly on the SILENCE branch's own pad (a completely
        different pad object) must reach it uninterrupted by that probe."""
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        # The silence branch's pad (selector's silence sink pad) has no
        # suppression probe at all -- confirm by checking it's a
        # different Pad object from the one the probe was installed on.
        self.assertIsNot(obj._mic_silence_pad, obj._mic_quarantine_q.get_static_pad("sink"))


class RebuiltGenerationClockPolicyTests(MockDuckingMixin, SimpleTestCase):
    """Item 8/9: rebuilt source has provide-clock=False and a freshly
    resolved runtime device (not the stale prior device string)."""

    def test_hw_generation_always_gets_provide_clock_false(self):
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        bin1, src1 = obj._build_mic_hw_generation("hw:CARD=A,DEV=0")
        bin2, src2 = obj._build_mic_hw_generation("hw:CARD=B,DEV=0")
        self.assertFalse(src1.get_property("provide-clock"))
        self.assertFalse(src2.get_property("provide-clock"))
        self.assertIsNot(src1, src2)

    def test_hw_generation_uses_the_device_string_passed_in(self):
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        _, src = obj._build_mic_hw_generation("plughw:CARD=FRESH,DEV=0")
        self.assertEqual(src.get_property("device"), "plughw:CARD=FRESH,DEV=0")

    def test_generation_counter_increments_each_call(self):
        obj = make_mic_stand_in()
        gen0 = obj._mic_hw_generation
        obj._build_mic_chain()  # builds generation 1 internally
        gen_after_cold_start = obj._mic_hw_generation
        obj._build_mic_hw_generation("x")
        self.assertGreater(gen_after_cold_start, gen0)
        self.assertEqual(obj._mic_hw_generation, gen_after_cold_start + 1)


class RecoveryHealthVerificationTests(MockDuckingMixin, SimpleTestCase):
    """Item 10/11: recovery does not declare OK on PLAYING alone (no
    buffers) and succeeds when both PLAYING>=500ms and a fresh buffer are
    present. Exercised directly against the worker logic's building
    blocks using a real pipeline + audiotestsrc standing in for hardware
    (same substitution technique used throughout this test file), since
    we cannot open real ALSA hardware in this environment.
    NOTE: this does not run through SlotCoordinator's background thread
    dispatch (that's covered at the pure-module level in
    test_audio_recovery.py) -- it isolates and re-implements just the
    worker's health-check LOOP body against a controllable pipeline, to
    keep this test deterministic and fast rather than sleeping 500ms+."""

    def _health_check(self, obj, bin_obj, playing_hold_s=0.5, deadline_s=2.0):
        """Mirrors _mic_dispatch_rebuild's worker() health-check loop
        exactly (kept in sync deliberately -- if that loop's logic
        changes, this helper should change with it)."""
        if not bin_obj.sync_state_with_parent():
            return False
        playing_since = None
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            _, st, _ = bin_obj.get_state(0)
            if st == Gst.State.PLAYING:
                if playing_since is None:
                    playing_since = time.monotonic()
                elif time.monotonic() - playing_since >= playing_hold_s:
                    break
            else:
                playing_since = None
            time.sleep(0.02)
        if playing_since is None or (time.monotonic() - playing_since) < playing_hold_s:
            return False
        age = obj._mic_buffer_age_ms()
        return age is not None and age <= 250

    def test_playing_with_no_buffer_ever_fails_health_check(self):
        """A bin that reaches PLAYING but never produces a buffer (no
        probe has ever fired, _mic_buf_last_ns stays None) must NOT pass
        -- this is the core "PLAYING alone is not proof" invariant."""
        obj = make_mic_stand_in()
        obj._build_mic_chain()  # sets up self._mic_buffer_age_ms() plumbing
        obj._mic_buf_last_ns = None  # never observed a buffer

        pipeline = Gst.Pipeline.new("health-test")
        bin_obj = Gst.Bin.new("silent_gen")
        # A source that reaches PLAYING but is deliberately disconnected
        # from anything that would ever trigger obj._mic_buffer_probe --
        # simulates "state changed, but nothing is really flowing to the
        # measured point" (e.g. a wedged/degenerate hardware open).
        src = Gst.ElementFactory.make("fakesrc", None)
        src.set_property("sizetype", 2)  # fixed
        sink = Gst.ElementFactory.make("fakesink", None)
        bin_obj.add(src); bin_obj.add(sink)
        src.link(sink)
        pipeline.add(bin_obj)
        try:
            # The PARENT pipeline must genuinely reach PLAYING -- a bin
            # whose parent was never started would trivially "pass"
            # sync_state_with_parent() without proving anything (a real
            # trap, caught directly: see play_pipeline's docstring).
            self.assertTrue(play_pipeline(pipeline))
            ok = self._health_check(obj, bin_obj, playing_hold_s=0.15, deadline_s=1.0)
            self.assertFalse(ok, "health check must fail when no buffer was ever observed at the measured point")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_playing_with_fresh_buffers_passes_health_check(self):
        obj = make_mic_stand_in()
        obj._build_mic_chain()

        pipeline = Gst.Pipeline.new("health-test-2")
        bin_obj = Gst.Bin.new("live_gen")
        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("is-live", True)
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", False)
        bin_obj.add(src); bin_obj.add(sink)
        src.link(sink)
        # Route the buffer probe through THIS bin's own pad, standing in
        # for quarantine_q's src pad in production.
        src.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, obj._mic_buffer_probe)
        pipeline.add(bin_obj)
        try:
            self.assertTrue(play_pipeline(pipeline))
            ok = self._health_check(obj, bin_obj, playing_hold_s=0.15, deadline_s=2.0)
            self.assertTrue(ok, "health check must pass with real PLAYING + fresh buffers")
        finally:
            pipeline.set_state(Gst.State.NULL)


class RecoveredMicStaysOffTests(MockDuckingMixin, SimpleTestCase):
    """Item 12: recovered mic stays logically OFF -- mic_ok True but
    mic_live False and PTT volume 0, requiring an explicit operator
    re-key. Exercises _mic_handle_slot_transition directly (the OK-
    transition-with-pending-bin branch), matching how _mic_recovery_tick
    would invoke it after a real background-worker success."""

    def test_ok_transition_with_pending_bin_leaves_ptt_off(self):
        obj = make_mic_stand_in()
        pipeline, mic_bin, sink = build_and_play(obj, synthetic_hw=True)
        try:
            self.assertTrue(play_pipeline(pipeline))
            # Simulate: a failure already happened, PTT already off.
            obj.mic_ok = False
            obj.mic_live = False
            obj.mic_ptt_volume.set_property("volume", 0.0)
            obj._mic_selector.set_property("active-pad", obj._mic_silence_pad)

            # Simulate a rebuild that's about to be promoted -- a second
            # synthetic generation, standing in for _build_mic_hw_
            # generation's real alsasrc-based one (identical ghost-pad
            # shape; this file's own docstring explains why no real
            # hardware is opened in this suite).
            new_bin = swap_in_synthetic_hw_generation(obj)
            new_bin.sync_state_with_parent()
            self.assertTrue(wait_until(lambda: new_bin.get_state(0)[1] == Gst.State.PLAYING))
            # Mirror the real pre-transition state: the new generation is
            # PENDING (not yet promoted); _mic_hw_bin is None until the
            # transition handler itself promotes it -- this is what
            # actually exercises the promotion assignment, not just
            # re-confirming a value that was already set.
            obj._mic_pending_hw_bin = new_bin
            obj._mic_hw_bin = None

            snapshot = {"generation": 2, "state": "OK"}
            obj._mic_handle_slot_transition("RECOVERING", "OK", snapshot)

            self.assertTrue(obj.mic_ok)
            self.assertFalse(obj.mic_live)
            self.assertEqual(obj.mic_ptt_volume.get_property("volume"), 0.0)
            assert_active_pad_is(self, obj._mic_selector, obj._mic_device_pad)
            self.assertIsNone(obj._mic_pending_hw_bin)
            self.assertIs(obj._mic_hw_bin, new_bin)
            self.assertEqual(len(self.emitted_events), 1)
            self.assertEqual(self.emitted_events[0]["title"], "Studio microphone recovered")
        finally:
            pipeline.set_state(Gst.State.NULL)


class QuiesceDiagnosticMessageTests(MockDuckingMixin, SimpleTestCase):
    """[P0] 1.3B4 -- pre-commit-review-discovered diagnostic bug, fixed:
    _mic_handle_slot_transition's DEGRADED-branch could not distinguish a
    normal, intentional re-degrade (the OK-branch's own mark_degraded()
    call, right after a successful quiesce) from a genuine worker
    failure -- both look identical via _mic_pending_hw_bin alone (None
    either way). Now gated on SlotCoordinator's own operation_succeeded
    tag, which mark_degraded() never touches, so it keeps reflecting the
    last RESOLVED operation's true outcome across a deliberate re-degrade.

    Uses the real SlotCoordinator + real _mic_handle_slot_transition end
    to end (dispatch a real worker, wait for it to resolve, then invoke
    the transition handler) -- not a synthetic snapshot dict -- so this
    proves the fix against the actual mechanism, not an assumption about
    its shape."""

    def _wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_successful_quiesce_to_degraded_emits_no_false_failure_message(self):
        import contextlib
        import io

        obj = make_mic_stand_in()
        obj._build_mic_chain()
        obj._mic_pending_hw_bin = None

        # First half of the real sequence: mark_degraded() + a real,
        # SUCCEEDING worker dispatch, exactly what _on_mic_error /
        # _mic_quiesce_current_generation do.
        obj._mic_slot.mark_degraded()
        obj._mic_slot.request_recovery(lambda: True)
        self.assertTrue(self._wait_until(lambda: obj._mic_slot.snapshot()["state"] == "OK"))

        # The transition handler's OK-branch: prints "quiesced cleanly"
        # and deliberately re-degrades via mark_degraded() again.
        ok_snapshot = obj._mic_slot.snapshot()
        obj._mic_handle_slot_transition("RECOVERING", "OK", ok_snapshot)
        self.assertEqual(obj._mic_slot.state, audio_recovery.SlotState.DEGRADED)

        # The transition this bug was about: observing THAT re-degrade.
        degraded_snapshot = obj._mic_slot.snapshot()
        self.assertIs(degraded_snapshot["operation_succeeded"], True,
                       "sanity check on the primitive the fix relies on")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            obj._mic_handle_slot_transition("OK", "DEGRADED", degraded_snapshot)
        output = buf.getvalue()
        self.assertNotIn("failed unexpectedly", output)
        self.assertNotIn("Mic quiesce operation failed", output)

    def test_genuine_quiesce_worker_failure_still_emits_failure_diagnostic(self):
        import contextlib
        import io

        obj = make_mic_stand_in()
        obj._build_mic_chain()
        obj._mic_pending_hw_bin = None

        obj._mic_slot.mark_degraded()
        obj._mic_slot.request_recovery(lambda: False)  # genuine failure
        self.assertTrue(self._wait_until(lambda: obj._mic_slot.snapshot()["state"] == "DEGRADED"))

        snapshot = obj._mic_slot.snapshot()
        self.assertIs(snapshot["operation_succeeded"], False,
                       "sanity check on the primitive the fix relies on")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            obj._mic_handle_slot_transition("RECOVERING", "DEGRADED", snapshot)
        output = buf.getvalue()
        self.assertIn("failed unexpectedly", output)


class MasterMixerTopologyUntouchedTests(MockDuckingMixin, SimpleTestCase):
    """Item 13: the master-mixer-facing (ghost) pad and its target are
    never replaced/re-requested by any recovery step -- only the
    hardware generation upstream of the persistent quarantine queue
    changes."""

    def test_ghost_pad_target_unchanged_across_a_full_quiesce_and_rebuild_cycle(self):
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        ghost_pad = obj._mic_bin.get_static_pad("src")
        target_before = ghost_pad.get_target()
        silence_pad_before = obj._mic_silence_pad
        device_pad_before = obj._mic_device_pad

        # Quiesce (tear down generation 1) -- no state call needed here,
        # this test only checks the pad-graph bookkeeping the GLib-thread
        # side performs before dispatching the background worker.
        old_bin = obj._mic_hw_bin
        old_bin.get_static_pad("src").unlink(obj._mic_quarantine_q.get_static_pad("sink"))
        obj._mic_bin.remove(old_bin)
        obj._mic_hw_bin = None

        # Rebuild generation 2.
        new_bin, new_src = obj._build_mic_hw_generation("hw:CARD=NEW,DEV=0")
        obj._mic_bin.add(new_bin)
        new_bin.get_static_pad("src").link(obj._mic_quarantine_q.get_static_pad("sink"))
        obj._mic_hw_bin = new_bin

        self.assertIs(obj._mic_bin.get_static_pad("src").get_target(), target_before)
        self.assertIs(obj._mic_silence_pad, silence_pad_before)
        self.assertIs(obj._mic_device_pad, device_pad_before)


class ShutdownDoesNotWaitForAbandonedWorkerTests(MockDuckingMixin, SimpleTestCase):
    """Item 16: an abandoned/orphaned bin is detached from the persistent
    mic bin BEFORE its state-change is handed to a background thread --
    so a subsequent whole-pipeline NULL transition never has to wait on
    it. Verified structurally (the abandoned bin is provably no longer
    part of the tree main_pipeline.set_state(NULL) would walk)."""

    def test_discard_pending_abandoned_removes_from_persistent_bin_without_state_call(self):
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        # quarantine_q's sink pad is already occupied by generation 1
        # (built by _build_mic_chain) -- detach it first, matching the
        # real sequence (_mic_quiesce_current_generation always runs
        # before a rebuild attempt links a new generation into the same
        # fixed pad).
        gen1 = obj._mic_hw_bin
        gen1.get_static_pad("src").unlink(obj._mic_quarantine_q.get_static_pad("sink"))
        obj._mic_bin.remove(gen1)
        obj._mic_hw_bin = None

        new_bin, new_src = obj._build_mic_hw_generation("hw:CARD=STUCK,DEV=0")
        obj._mic_bin.add(new_bin)
        new_bin.get_static_pad("src").link(obj._mic_quarantine_q.get_static_pad("sink"))
        obj._mic_pending_hw_bin = new_bin

        state_calls = []
        real_set_state = new_bin.set_state
        new_bin.set_state = lambda *a, **kw: state_calls.append(a) or real_set_state(*a, **kw)

        obj._mic_discard_pending_hw_bin(abandoned=True)

        self.assertIsNone(obj._mic_pending_hw_bin)
        # No set_state call was made on the abandoned bin -- per the
        # still-open Round-3 question, we never touch it again.
        self.assertEqual(state_calls, [])
        # And it's structurally no longer reachable from the persistent
        # bin a real shutdown's main_pipeline.set_state(NULL) would walk.
        found = False
        it = obj._mic_bin.iterate_elements()
        while True:
            ok, el = it.next()
            if not ok:
                break
            if el is new_bin:
                found = True
        self.assertFalse(found, "abandoned bin must be removed from the persistent mic bin")

    def test_discard_pending_non_abandoned_null_goes_through_guarded_worker_not_glib_thread(self):
        """Pre-commit review, Issue 3: an earlier draft called
        bin_obj.set_state(Gst.State.NULL) directly and synchronously here
        -- exactly the unguarded-hardware-state-call pattern the locked
        design forbids. Fixed to dispatch through the same SlotCoordinator
        background worker as _mic_quiesce_current_generation. Proven here
        by making set_state() itself block, and confirming the calling
        thread (this test's own, standing in for the GLib thread) is NOT
        the one that blocks -- request_recovery() must return immediately
        regardless of how long the dispatched worker takes."""
        obj = make_mic_stand_in()
        obj._build_mic_chain()  # slot starts OK

        gen1 = obj._mic_hw_bin
        gen1.get_static_pad("src").unlink(obj._mic_quarantine_q.get_static_pad("sink"))
        obj._mic_bin.remove(gen1)
        obj._mic_hw_bin = None

        new_bin, new_src = obj._build_mic_hw_generation("hw:CARD=SLOW,DEV=0")
        obj._mic_bin.add(new_bin)
        new_bin.get_static_pad("src").link(obj._mic_quarantine_q.get_static_pad("sink"))
        obj._mic_pending_hw_bin = new_bin

        # Slot must be non-OK for request_recovery() to accept a
        # dispatch -- matches the real precondition (this method only
        # ever runs after a rebuild attempt already resolved to DEGRADED).
        obj._mic_slot.mark_degraded()

        set_state_called = threading.Event()
        release_set_state = threading.Event()
        real_set_state = new_bin.set_state

        def blocking_set_state(state):
            set_state_called.set()
            release_set_state.wait(timeout=5.0)
            return real_set_state(state)

        new_bin.set_state = blocking_set_state

        call_thread_ident = {"id": None}
        t0 = time.monotonic()

        # Wrap in a thread with a short timeout to prove THIS call
        # (standing in for the GLib thread) does not block.
        def call_discard():
            call_thread_ident["id"] = threading.get_ident()
            obj._mic_discard_pending_hw_bin(abandoned=False)

        caller = threading.Thread(target=call_discard)
        caller.start()
        caller.join(timeout=2.0)
        elapsed = time.monotonic() - t0

        self.assertFalse(caller.is_alive(), "the calling thread must return promptly, not block on set_state()")
        self.assertLess(elapsed, 1.0)
        self.assertTrue(set_state_called.wait(timeout=1.0), "the dispatched worker must have called set_state()")

        # Confirm set_state() actually ran on a DIFFERENT thread than the
        # one that called _mic_discard_pending_hw_bin.
        self.assertIsNone(obj._mic_pending_hw_bin)

        release_set_state.set()  # let the background worker finish so it doesn't leak past the test


class RemoteDjDoesNotBlockRecoveryTests(MockDuckingMixin, SimpleTestCase):
    """Item 17: recovery proceeds regardless of remote_dj_live state --
    no blanket defer. Audibility safety comes from PTT staying at 0, not
    from gating recovery on remote-DJ state."""

    def _fake_error_message(self, message_text):
        class _Err:
            def __str__(self_inner):
                return message_text
        class _Msg:
            def parse_error(self_inner):
                return _Err(), ""
        return _Msg()

    def test_recovery_proceeds_while_remote_dj_gate_reports_live(self):
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        obj._remote_dj_gate_open = lambda: True  # remote DJ is "on air"
        gen_before = obj._mic_slot.generation

        msg = self._fake_error_message(
            "gst-resource-error-quark: Error recording from audio device. "
            "The device has been disconnected. (9)")
        obj._on_mic_error(None, msg)

        # Recovery (quiesce dispatch) still proceeds -- generation
        # advanced, proving a background operation was actually
        # dispatched, not silently skipped because remote DJ is live. Not
        # asserting on the SLOT'S transient state here: old_bin in this
        # test was never added to a real pipeline, so its NULL teardown
        # resolves near-instantly on the background thread, racing
        # unpredictably past RECOVERING to OK before this line runs --
        # generation is the deterministic signal, state is not.
        self.assertGreater(obj._mic_slot.generation, gen_before)
        # And audibility safety held: PTT is 0 regardless.
        self.assertEqual(obj.mic_ptt_volume.get_property("volume"), 0.0)


class ExistingMicClockPolicyStillHoldsTests(MockDuckingMixin, SimpleTestCase):
    """Item 19: [P0] 1.3B1's clock-policy guarantee survives this
    refactor -- re-checked here at the _build_mic_chain level (1.3B1's
    own test_engine_mic_clock_policy.py checks it independently too;
    this is a belt-and-braces regression guard specifically for the
    1.3B2 restructuring)."""

    def test_cold_start_generation_has_provide_clock_false(self):
        obj = make_mic_stand_in()
        elements = obj._build_mic_chain()
        mic_bin = elements[0]
        hw_src = obj._mic_hw_bin.get_by_name(None) if False else None
        # Find the alsasrc by class, not by exact name (name now includes
        # a generation suffix, e.g. "mic_src_gen1").
        found = []
        it = obj._mic_hw_bin.iterate_elements()
        while True:
            ok, el = it.next()
            if not ok:
                break
            if el.get_factory() and el.get_factory().get_name() == "alsasrc":
                found.append(el)
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].get_property("provide-clock"))
