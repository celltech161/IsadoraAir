"""Regression coverage for the branch-local FX/VT completion mechanism
(2026-08 pass) that replaced the old bus-based _fx_on_eos.

Root cause (confirmed live and via an isolated offline harness before
this fix, not guessed): fx_submix is a live GstAggregator with other
permanently-running inputs (its own permanent silence branch), so a
GstAggregator's pipeline-wide EOS -- which only posts once EVERY sink
pad has seen EOS -- can never fire. The old bus.connect("message::eos",
self._fx_on_eos, fire_id) therefore never ran for a naturally-completed
fire, leaking it in self._fx_fires forever (confirmed live: a ~6s cart
still present in state past 70s).

The fix installs a branch-local pad probe (_fx_install_completion_probe,
called from both _fx_fire and _vt_fire_file) on the last real element's
src pad before it joins fx_submix, mirroring _create_deck's own
eos_probe (identical problem, identical fix, already proven safe in
this exact codebase for self.mixer). On EOS it schedules
_fx_fire_completed via GLib.idle_add (streaming-thread -> main-loop
handoff) and drops the event before it reaches fx_submix.

These tests never build a real Gst.Pipeline -- Gst.ElementFactory.make
is patched to hand back MagicMocks (library.tests.test_fx_fires_state's
own established approach), so gain.get_static_pad("src") is a stable
mock pad across calls (MagicMock's own return_value caching), letting
tests capture the EXACT callback _fx_install_completion_probe
registered via pad.add_probe(...) and invoke it directly with a
simulated EOS event -- exercising the real production callback object,
not a reimplementation of it. GLib.idle_add itself is real (not
mocked); tests drain it via a real GLib.MainContext iteration so the
streaming-thread -> main-loop handoff is exercised for real too."""
import threading
import time
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase

import library.services.engine as eng_module
from library.models import FXBusConfig, FXCart

eng_module.Gst.init(None)


def make_stand_in():
    obj = object.__new__(eng_module.PlaybackEngine)
    obj._fx_fires = {}
    obj._fx_lock = threading.Lock()
    obj._fx_next_id = 1
    obj.main_pipeline = MagicMock()
    obj.fx_submix = MagicMock()
    obj.pipeline_sample_rate = 44100
    obj._vt_lock = threading.Lock()
    obj._vt = {"phase": "idle"}
    return obj


def make_cart(**overrides):
    import tempfile
    defaults = dict(name="Test Cart", gain_db=0.0, retrigger_mode="restart", enabled=True)
    defaults.update(overrides)
    if "filepath" not in defaults:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(b"\x00" * 16)
        tmp.close()
        defaults["filepath"] = tmp.name
    return FXCart.objects.create(**defaults)


def _patched_element_factory():
    return patch.object(eng_module.Gst.ElementFactory, "make", side_effect=lambda *a, **k: MagicMock())


def _capture_completion_probe(stand_in, fire_id):
    """Pulls the exact callback _fx_install_completion_probe registered
    via pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, callback) on
    this fire's gain element's src pad."""
    state = stand_in._fx_fires[fire_id]
    gain_src_pad = state["gain"].get_static_pad("src")
    call = gain_src_pad.add_probe.call_args
    assert call is not None, f"no completion probe was registered for fire {fire_id}"
    probe_type, callback = call.args
    assert probe_type == eng_module.Gst.PadProbeType.EVENT_DOWNSTREAM
    return callback, gain_src_pad


def _fake_eos_probe_info():
    fake_event = MagicMock()
    fake_event.type = eng_module.Gst.EventType.EOS
    info = MagicMock()
    info.get_event.return_value = fake_event
    return info


def _fake_non_eos_probe_info():
    fake_event = MagicMock()
    fake_event.type = eng_module.Gst.EventType.SEGMENT
    info = MagicMock()
    info.get_event.return_value = fake_event
    return info


def _drain_glib_idle_queue():
    """Actually runs whatever GLib.idle_add scheduled -- real GLib main
    context iteration, not a mock of idle_add itself, so the streaming-
    thread -> main-loop handoff is exercised for real."""
    ctx = eng_module.GLib.MainContext.default()
    # A few iterations: idle_add's own callback can itself be quick,
    # but bound the loop so a bug (e.g. a callback that reschedules
    # itself) can't hang the test suite.
    for _ in range(20):
        if not ctx.iteration(False):
            break


class OrdinaryFxNaturalCompletionTests(TransactionTestCase):
    def setUp(self):
        self.stand_in = make_stand_in()

    def test_eos_probe_registered_and_drops_event(self):
        cart = make_cart()
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
        (fire_id,) = list(self.stand_in._fx_fires.keys())
        callback, pad = _capture_completion_probe(self.stand_in, fire_id)

        result = callback(pad, _fake_eos_probe_info())

        self.assertEqual(result, eng_module.Gst.PadProbeReturn.DROP)

    def test_non_eos_event_passes_through_untouched(self):
        cart = make_cart()
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
        (fire_id,) = list(self.stand_in._fx_fires.keys())
        callback, pad = _capture_completion_probe(self.stand_in, fire_id)

        result = callback(pad, _fake_non_eos_probe_info())

        self.assertEqual(result, eng_module.Gst.PadProbeReturn.OK)
        # Must not have scheduled completion for an unrelated event.
        self.assertIn(fire_id, self.stand_in._fx_fires)

    def test_natural_completion_removes_fire_from_state(self):
        cart = make_cart()
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
        (fire_id,) = list(self.stand_in._fx_fires.keys())
        callback, pad = _capture_completion_probe(self.stand_in, fire_id)
        state = self.stand_in._fx_fires[fire_id]

        callback(pad, _fake_eos_probe_info())
        _drain_glib_idle_queue()

        self.assertNotIn(fire_id, self.stand_in._fx_fires)
        self.assertEqual(self.stand_in._fx_fires_state(), [])
        # Mixer pad released and branch elements torn down.
        self.stand_in.fx_submix.release_request_pad.assert_called_once_with(state["fx_pad"])
        state["gain"].set_state.assert_called_once_with(eng_module.Gst.State.NULL)
        state["filesrc"].set_state.assert_called_once_with(eng_module.Gst.State.NULL)
        self.stand_in.main_pipeline.remove.assert_any_call(state["gain"])
        self.stand_in.main_pipeline.remove.assert_any_call(state["filesrc"])

    def test_does_not_affect_other_simultaneous_fires(self):
        cart_a = make_cart(name="A")
        cart_b = make_cart(name="B")
        with _patched_element_factory():
            self.stand_in._fx_fire(cart_a.id)
            self.stand_in._fx_fire(cart_b.id)
        fire_ids = list(self.stand_in._fx_fires.keys())
        self.assertEqual(len(fire_ids), 2)
        fire_a = next(fid for fid, s in self.stand_in._fx_fires.items() if s["cart_id"] == cart_a.id)
        callback, pad = _capture_completion_probe(self.stand_in, fire_a)

        callback(pad, _fake_eos_probe_info())
        _drain_glib_idle_queue()

        state = self.stand_in._fx_fires_state()
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]["cart_id"], cart_b.id)  # untouched


class VtNaturalCompletionTests(TransactionTestCase):
    def setUp(self):
        self.stand_in = make_stand_in()

    def _fire_vt(self, kind="outro"):
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(b"\x00" * 16)
        tmp.close()
        with _patched_element_factory():
            fire_id = self.stand_in._vt_fire_file(tmp.name, 0.0, kind)
        return fire_id

    def test_vt_completion_invokes_vt_on_fire_eos_before_fx_stop(self):
        fire_id = self._fire_vt(kind="outro")
        callback, pad = _capture_completion_probe(self.stand_in, fire_id)

        call_order = []
        original_vt_on_fire_eos = self.stand_in._vt_on_fire_eos

        def spy_vt_on_fire_eos(kind, fid):
            # Fire must still be present in _fx_fires at the moment VT
            # state advances -- _vt_handle_outgoing_ended's own check
            # (outro_fire_id in self._fx_fires) depends on this.
            call_order.append(("vt_advance", fid in self.stand_in._fx_fires))
            return original_vt_on_fire_eos(kind, fid)

        def spy_fx_stop(fid):
            call_order.append(("fx_stop", fid in self.stand_in._fx_fires))
            return eng_module.PlaybackEngine._fx_stop(self.stand_in, fid)

        with patch.object(self.stand_in, "_vt_on_fire_eos", side_effect=spy_vt_on_fire_eos), \
             patch.object(self.stand_in, "_fx_stop", side_effect=spy_fx_stop):
            callback(pad, _fake_eos_probe_info())
            _drain_glib_idle_queue()

        self.assertEqual(call_order, [("vt_advance", True), ("fx_stop", True)])

    def test_vt_completion_removes_fire_from_state(self):
        fire_id = self._fire_vt(kind="intro")
        callback, pad = _capture_completion_probe(self.stand_in, fire_id)

        callback(pad, _fake_eos_probe_info())
        _drain_glib_idle_queue()

        self.assertNotIn(fire_id, self.stand_in._fx_fires)

    def test_vt_kind_and_fire_id_passed_correctly(self):
        fire_id = self._fire_vt(kind="outro")
        callback, pad = _capture_completion_probe(self.stand_in, fire_id)

        with patch.object(self.stand_in, "_vt_on_fire_eos") as mock_advance:
            callback(pad, _fake_eos_probe_info())
            _drain_glib_idle_queue()

        mock_advance.assert_called_once_with("outro", fire_id)


class DuplicateCompletionTests(TransactionTestCase):
    def setUp(self):
        self.stand_in = make_stand_in()

    def test_completion_observed_twice_is_a_safe_noop_second_time(self):
        cart = make_cart()
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
        (fire_id,) = list(self.stand_in._fx_fires.keys())
        callback, pad = _capture_completion_probe(self.stand_in, fire_id)

        callback(pad, _fake_eos_probe_info())
        _drain_glib_idle_queue()
        release_calls_after_first = self.stand_in.fx_submix.release_request_pad.call_count

        # Simulate a duplicate/late EOS notification for the same fire.
        callback(pad, _fake_eos_probe_info())
        _drain_glib_idle_queue()

        # No exception (implicit -- would have failed the test), and no
        # second release/teardown.
        self.assertEqual(
            self.stand_in.fx_submix.release_request_pad.call_count,
            release_calls_after_first,
        )

    def test_stale_fire_id_completion_is_a_safe_noop(self):
        # Directly exercises _fx_fire_completed with a fire_id that was
        # never registered at all.
        result = self.stand_in._fx_fire_completed(999999)
        self.assertFalse(result)  # idle_add: don't reschedule
        self.assertEqual(self.stand_in._fx_fires, {})

    def test_retrigger_teardown_races_ahead_of_queued_completion(self):
        # Fire, then let a synchronous retrigger ('restart') tear it
        # down BEFORE the (already-scheduled) natural-completion
        # callback ever runs -- the classic race the task called out.
        cart = make_cart(retrigger_mode="restart")
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
        (first_fire_id,) = list(self.stand_in._fx_fires.keys())
        callback, pad = _capture_completion_probe(self.stand_in, first_fire_id)

        # Simulate the probe firing (schedules completion) WITHOUT
        # draining the idle queue yet.
        callback(pad, _fake_eos_probe_info())

        # Now a retrigger beats it to the punch, synchronously.
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
        self.assertNotIn(first_fire_id, self.stand_in._fx_fires)  # already torn down by retrigger
        (second_fire_id,) = list(self.stand_in._fx_fires.keys())
        self.assertNotEqual(first_fire_id, second_fire_id)

        # The queued completion for the FIRST fire finally runs -- must
        # not touch the second (already-replaced) fire.
        _drain_glib_idle_queue()

        self.assertIn(second_fire_id, self.stand_in._fx_fires)  # untouched by the stale completion
        state = self.stand_in._fx_fires_state()
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]["cart_id"], cart.id)


class PolyphonyRecoveryTests(TransactionTestCase):
    """Directly covers the live failure mode this whole fix exists for:
    completed fires must actually free their polyphony-cap slot."""

    def setUp(self):
        self.stand_in = make_stand_in()
        FXBusConfig.objects.filter(pk=1).delete()
        FXBusConfig.objects.create(pk=1, polyphony_cap=2)

    def test_cap_recovers_after_natural_completion_and_new_fire_succeeds(self):
        cart_a = make_cart(name="A")
        cart_b = make_cart(name="B")
        cart_c = make_cart(name="C")

        with _patched_element_factory():
            self.assertTrue(self.stand_in._fx_fire(cart_a.id))
            self.assertTrue(self.stand_in._fx_fire(cart_b.id))
            # Cap (2) is hit -- third distinct cart is rejected.
            self.assertFalse(self.stand_in._fx_fire(cart_c.id))

        self.assertEqual(self.stand_in._fx_active_count(), 2)

        # Naturally complete BOTH active fires.
        for fire_id in list(self.stand_in._fx_fires.keys()):
            callback, pad = _capture_completion_probe(self.stand_in, fire_id)
            callback(pad, _fake_eos_probe_info())
        _drain_glib_idle_queue()

        self.assertEqual(self.stand_in._fx_active_count(), 0)
        self.assertEqual(self.stand_in._fx_fires_state(), [])

        # A cart that was previously rejected by the cap must now fire
        # successfully -- proves the slot genuinely freed, not just
        # that it stopped growing.
        with _patched_element_factory():
            self.assertTrue(self.stand_in._fx_fire(cart_c.id))
        self.assertEqual(self.stand_in._fx_active_count(), 1)


class AuthoritativeStateIntegrationTests(TransactionTestCase):
    """Confirms the original _fx_fires_state() tests' promise actually
    holds end-to-end now that natural completion works: a naturally
    completed fire disappears from serialized state because it has
    genuinely left self._fx_fires, not just because of a test-level
    assumption."""

    def setUp(self):
        self.stand_in = make_stand_in()

    def test_fire_appears_then_vanishes_from_authoritative_state(self):
        cart = make_cart()
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
        (fire_id,) = list(self.stand_in._fx_fires.keys())

        mid_flight = self.stand_in._fx_fires_state()
        self.assertEqual(len(mid_flight), 1)
        self.assertEqual(mid_flight[0]["cart_id"], cart.id)

        callback, pad = _capture_completion_probe(self.stand_in, fire_id)
        callback(pad, _fake_eos_probe_info())
        _drain_glib_idle_queue()

        self.assertEqual(self.stand_in._fx_fires_state(), [])
