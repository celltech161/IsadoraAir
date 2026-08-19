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
    policy and its real version also hits the DB).

    [P0] 1.3B2 update: _build_mic_chain now also builds the persistent
    silence/quarantine/selector topology and an initial hardware
    generation via _build_mic_hw_generation -- the extra attributes below
    are what those additions touch. See test_engine_mic_recovery.py for
    the full 1.3B2-specific test suite; this file stays focused on the
    1.3B1 clock-policy guarantee alone, now re-verified against the
    post-1.3B2 construction shape."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj._resolve_mic_device = lambda: FAKE_MIC_DEVICE
    obj._resolve_mic_identity = lambda: None  # real version hits the DB; harmless no-op here
    obj._mic_identity_kind = ""
    obj._mic_identity = ""
    obj._mic_legacy_device = FAKE_MIC_DEVICE
    obj.pipeline_sample_rate = 44100
    obj._apply_mic_gain = lambda: None
    obj.mic_gain = None
    obj.mic_ptt_valve = None
    obj.mic_ptt_volume = None
    obj._mic_bin = None
    obj.mic_ok = False
    obj._mic_hw_bin = None
    obj._mic_hw_generation = 0
    obj._mic_pending_hw_bin = None
    obj._mic_selector = None
    obj._mic_silence_pad = None
    obj._mic_device_pad = None
    obj._mic_quarantine_q = None
    obj._mic_slot = None
    obj._mic_last_observed_slot_state = None
    obj._mic_buf_last_ns = None
    obj._mic_buf_count = 0
    return obj


def get_alsasrc(mic_bin):
    """The mic bin's alsasrc, found by element FACTORY TYPE rather than
    exact name or position. [P0] 1.3B2 update: the alsasrc now lives
    nested inside a per-generation sub-bin (e.g. "mic_hw_gen1") and is
    itself named with a generation suffix (e.g. "mic_src_gen1") -- exact-
    name lookup would silently break on every rebuild. Gst.Bin.get_by_
    name()/iterate_elements() both recurse into nested bins, so this
    still finds it regardless of nesting depth or generation number."""
    it = mic_bin.iterate_recurse()
    while True:
        ok, el = it.next()
        if not ok:
            return None
        factory = el.get_factory()
        if factory is not None and factory.get_name() == "alsasrc":
            return el


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
    """Item 4, re-scoped for [P0] 1.3B2: the mixer-facing DOWNSTREAM half
    of the chain (mic_gain -> mic_ptt_volume -> ghost pad) is genuinely
    unchanged -- this phase's own locked design explicitly requires it to
    stay that way ("mixer-facing side is persistent"). The UPSTREAM half
    (alsasrc -> convert -> resample -> caps) intentionally moved into a
    nested, per-generation sub-bin behind a persistent silence/quarantine/
    selector boundary -- that restructuring is the whole point of 1.3B2,
    not a regression, so this test now asserts the NEW shape rather than
    pinning the OLD one. See test_engine_mic_recovery.py for exhaustive
    coverage of the new persistent/replaceable split itself."""

    def test_downstream_half_link_order_unchanged(self):
        obj = make_mic_stand_in()
        mic_bin = obj._build_mic_chain()[0]

        gain = mic_bin.get_by_name("mic_gain")
        ptt = mic_bin.get_by_name("mic_ptt_volume")
        self.assertIsNotNone(gain)
        self.assertIsNotNone(ptt)

        def linked(upstream, downstream):
            pad = upstream.get_static_pad("src")
            peer = pad.get_peer()
            return peer is not None and peer.get_parent_element() is downstream

        self.assertTrue(linked(gain, ptt))

    def test_upstream_half_reaches_gain_through_persistent_boundary(self):
        """alsasrc -> convert -> resample -> caps -> (ghost) -> quarantine
        queue -> input-selector -> mic_gain -- the new persistent
        boundary this phase adds, verified end-to-end by pad linkage
        rather than assuming it."""
        obj = make_mic_stand_in()
        mic_bin = obj._build_mic_chain()[0]

        src = get_alsasrc(mic_bin)
        self.assertIsNotNone(src)
        convert = src.get_static_pad("src").get_peer().get_parent_element()
        self.assertEqual(convert.get_factory().get_name(), "audioconvert")
        resample = convert.get_static_pad("src").get_peer().get_parent_element()
        self.assertEqual(resample.get_factory().get_name(), "audioresample")
        capsfilter = resample.get_static_pad("src").get_peer().get_parent_element()
        self.assertEqual(capsfilter.get_factory().get_name(), "capsfilter")

        # The hw generation's ghost "src" pad feeds the persistent
        # quarantine queue's sink pad directly.
        hw_bin = obj._mic_hw_bin
        self.assertIsNotNone(hw_bin)
        quarantine_sink = obj._mic_quarantine_q.get_static_pad("sink")
        self.assertIs(hw_bin.get_static_pad("src").get_peer(), quarantine_sink)

        # quarantine_q -> input-selector's device pad -> mic_gain.
        selector = obj._mic_selector
        self.assertIs(
            obj._mic_quarantine_q.get_static_pad("src").get_peer(), obj._mic_device_pad)
        gain = mic_bin.get_by_name("mic_gain")
        self.assertIs(selector.get_static_pad("src").get_peer(), gain.get_static_pad("sink"))

    def test_silence_branch_present_and_feeds_selector(self):
        """New in 1.3B2, not a regression check on old behavior --
        confirms the persistent fallback exists at all."""
        obj = make_mic_stand_in()
        mic_bin = obj._build_mic_chain()[0]
        self.assertIsNotNone(obj._mic_silence_pad)
        silence_src = mic_bin.get_by_name("mic_silence_src")
        self.assertIsNotNone(silence_src)
        self.assertEqual(silence_src.get_factory().get_name(), "audiotestsrc")

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
