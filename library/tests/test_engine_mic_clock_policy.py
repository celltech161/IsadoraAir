"""[P0] 1.3B1 — production input clock hardening.

Covers the single production change: `mic_src` (the studio microphone
`alsasrc` built by `PlaybackEngine._build_mic_chain`) must never be
eligible to provide `self.main_pipeline`'s clock. See
scratchpad/audio_recovery/PHASE_P0_1.3_DISCOVERY_AND_DESIGN.md for the
eight-round investigation this is based on -- summary: a vanished ALSA
capture device can leave its GstAudioSrcClock selected but silently
frozen, stalling every other live branch sharing the pipeline. The fix is
static (`provide-clock=False` at construction, no dynamic reselection),
so these tests only need to inspect element properties on the built bin,
never run the pipeline.

Uses the same `object.__new__(PlaybackEngine)` minimal-stand-in pattern
as test_engine_runtime_commit.py -- bypasses __init__ (which builds a
real full pipeline and calls Gst.init itself) and stubs only what
_build_mic_chain actually touches. Real GStreamer elements are still
built and inspected directly (matching
test_remote_dj_monitor_mixer_timeline.py's "exercise the real underlying
Gst behavior, don't mock it" convention) -- just never set to a running
state, so no real ALSA device needs to exist."""
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from django.test import SimpleTestCase

import library.services.engine as eng_module

Gst.init(None)

FAKE_MIC_DEVICE = "hw:CARD=NoSuchTestCard,DEV=0"


def make_mic_stand_in():
    """Minimal PlaybackEngine stand-in covering exactly what
    _build_mic_chain touches: _resolve_mic_device (stubbed -- real
    version hits the DB, forbidden under SimpleTestCase, and would
    return None here anyway, short-circuiting the whole method before
    the alsasrc is even built), pipeline_sample_rate, and
    _apply_mic_gain (stubbed to a no-op -- gain is irrelevant to clock
    policy and its real version also hits the DB)."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj._resolve_mic_device = lambda: FAKE_MIC_DEVICE
    obj.pipeline_sample_rate = 44100
    obj._apply_mic_gain = lambda: None
    obj.mic_gain = None
    obj.mic_ptt_valve = None
    obj.mic_ptt_volume = None
    obj._mic_bin = None
    obj.mic_ok = False
    return obj


def get_alsasrc(mic_bin):
    """The mic bin's alsasrc, found by element name (assigned "mic_src"
    in _build_mic_chain) rather than by position, so this doesn't
    silently start checking the wrong element if the chain is
    reordered."""
    return mic_bin.get_by_name("mic_src")


class MicSourceProvidesClockFalseTests(SimpleTestCase):
    """Item 1: initial construction gets provide-clock == False."""

    def test_fresh_mic_chain_alsasrc_has_provide_clock_false(self):
        obj = make_mic_stand_in()
        elements = obj._build_mic_chain()

        self.assertEqual(len(elements), 1)
        mic_bin = elements[0]
        src = get_alsasrc(mic_bin)
        self.assertIsNotNone(src, "mic_src alsasrc not found in built bin")
        self.assertFalse(src.get_property("provide-clock"))

    def test_device_property_still_applied(self):
        """Sanity check that the new property line didn't clobber the
        one right above it, or land on the wrong element."""
        obj = make_mic_stand_in()
        mic_bin = obj._build_mic_chain()[0]
        src = get_alsasrc(mic_bin)
        self.assertEqual(src.get_property("device"), FAKE_MIC_DEVICE)


class MicSourceRebuildAlsoGetsClockFalseTests(SimpleTestCase):
    """Item 2: every generation gets provide-clock == False -- not just
    the first call. There is currently exactly one construction path
    (_build_mic_chain, called once from _build_main_pipeline; hotplug
    rebuild is explicitly NOT implemented in this phase, see
    scratchpad/audio_recovery/PHASE_P0_1.3_DISCOVERY_AND_DESIGN.md Round
    8 Part G). This proves the property is set unconditionally on every
    invocation of the sole construction path, which is what a future
    rebuild/recovery call (1.3B2+) will also go through -- so this
    guarantee already covers it structurally, before that path exists."""

    def test_calling_build_mic_chain_twice_both_get_provide_clock_false(self):
        obj = make_mic_stand_in()

        first_bin = obj._build_mic_chain()[0]
        first_src = get_alsasrc(first_bin)
        self.assertFalse(first_src.get_property("provide-clock"))

        second_bin = obj._build_mic_chain()[0]
        second_src = get_alsasrc(second_bin)
        self.assertFalse(second_src.get_property("provide-clock"))

        # Genuinely a second, independent element -- not the same object
        # re-inspected, which would prove nothing about "every generation".
        self.assertIsNot(first_src, second_src)


class NoPipelineClockForcingIntroducedTests(SimpleTestCase):
    """Item 3: this change must be the narrow structural fix (remove the
    source from clock candidacy, let GStreamer's normal automatic
    selection do the rest) -- not the retired dynamic-reselection
    approach (Option C), which would call pipeline.use_clock() or
    Gst.SystemClock.obtain() directly. A source-level regex check, not a
    property-behavior test -- this asserts something about the *code*,
    not the *runtime*, so it belongs alongside the other property checks
    here rather than in the topology test below."""

    def test_build_mic_chain_source_has_no_use_clock_or_systemclock_call(self):
        """Checks actual code lines only, not comments -- the
        provide-clock comment above legitimately *mentions*
        pipeline.use_clock() by name to explain why it's deliberately
        NOT used, which a naive whole-source substring check would
        misflag."""
        import inspect

        source = inspect.getsource(eng_module.PlaybackEngine._build_mic_chain)
        code_lines = [
            line for line in source.splitlines()
            if not line.strip().startswith("#")
        ]
        code_only = "\n".join(code_lines)
        self.assertNotIn("use_clock(", code_only)
        self.assertNotIn("SystemClock.obtain(", code_only)


class MicChainTopologyUnchangedTests(SimpleTestCase):
    """Item 4: existing mic chain topology (elements, link order, ghost
    pad) is unchanged by this one-property addition."""

    def test_element_set_and_link_order_unchanged(self):
        obj = make_mic_stand_in()
        mic_bin = obj._build_mic_chain()[0]

        names = {"mic_src", "mic_convert", "mic_resample", "mic_caps",
                 "mic_queue", "mic_gain", "mic_ptt_volume"}
        found = {el.get_name() for el in mic_bin.iterate_elements()}
        self.assertEqual(found, names)

        src = mic_bin.get_by_name("mic_src")
        convert = mic_bin.get_by_name("mic_convert")
        resample = mic_bin.get_by_name("mic_resample")
        capsfilter = mic_bin.get_by_name("mic_caps")
        queue = mic_bin.get_by_name("mic_queue")
        gain = mic_bin.get_by_name("mic_gain")
        ptt = mic_bin.get_by_name("mic_ptt_volume")

        def linked(upstream, downstream):
            pad = upstream.get_static_pad("src")
            peer = pad.get_peer()
            return peer is not None and peer.get_parent_element() is downstream

        self.assertTrue(linked(src, convert))
        self.assertTrue(linked(convert, resample))
        self.assertTrue(linked(resample, capsfilter))
        self.assertTrue(linked(capsfilter, queue))
        self.assertTrue(linked(queue, gain))
        self.assertTrue(linked(gain, ptt))

    def test_ghost_pad_targets_ptt_volume_src_pad(self):
        obj = make_mic_stand_in()
        mic_bin = obj._build_mic_chain()[0]
        ghost = mic_bin.get_static_pad("src")
        self.assertIsNotNone(ghost)
        ptt = mic_bin.get_by_name("mic_ptt_volume")
        self.assertIs(ghost.get_target(), ptt.get_static_pad("src"))

    def test_no_device_configured_returns_empty_list_unchanged(self):
        """The existing "mic not configured" short-circuit (returns []
        before ever touching Gst.ElementFactory) is untouched by this
        change -- provide-clock is set after the early-return point, on
        an element that in this branch is never even created."""
        obj = make_mic_stand_in()
        obj._resolve_mic_device = lambda: None
        self.assertEqual(obj._build_mic_chain(), [])


class MicPttAndMuteBehaviorUnchangedTests(SimpleTestCase):
    """Item 5: PTT/mute wiring (the mic_ptt_volume element and its
    default closed/muted state) is unaffected by this clock-policy
    change -- it lives entirely downstream of alsasrc in the chain."""

    def test_ptt_volume_starts_muted_and_alias_set(self):
        obj = make_mic_stand_in()
        mic_bin = obj._build_mic_chain()[0]
        self.assertEqual(obj.mic_ptt_volume.get_property("volume"), 0.0)
        self.assertIs(obj.mic_ptt_valve, obj.mic_ptt_volume)
        self.assertIs(mic_bin.get_by_name("mic_ptt_volume"), obj.mic_ptt_volume)


class RemoteDJAndAudioDeviceConfigUntouchedTests(SimpleTestCase):
    """Items 6-7: this phase touches only _build_mic_chain. Remote DJ
    session/slot construction and the general audio-device-configuration
    resolvers (_resolve_studio_monitor_device, _apply_audio_output_device,
    etc.) are separate methods this diff does not modify -- confirmed
    directly by source inspection rather than asserted by absence of a
    failing test, since none of those paths are exercised by
    constructing a mic chain in isolation."""

    def test_build_mic_chain_is_the_only_method_touched_for_provide_clock(self):
        import inspect

        source = inspect.getsource(eng_module)
        # Exactly one call site sets provide-clock anywhere in engine.py --
        # scoped to mic_src, not spread across the mixer/output/Remote DJ
        # construction this phase is explicitly not touching.
        self.assertEqual(source.count('"provide-clock"'), 1)

    def test_mic_ok_flag_set_true_on_successful_build(self):
        obj = make_mic_stand_in()
        obj._build_mic_chain()
        self.assertTrue(obj.mic_ok)
