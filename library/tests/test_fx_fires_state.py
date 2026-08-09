"""Regression coverage for engine-authoritative FX Cart playback state
(2026-08 pass): _fx_fires_state() (library/services/engine.py), which
_write_state exposes verbatim through /api/engine/status/ as
data["fx_fires"] for the dashboard's reconcileFxFires/_fxSyncButton to
consume, regardless of what triggered the fire -- a manual click, a
keyboard shortcut, another open dashboard/remote-DJ session, or an
external process like the weather beep bridge (fire_fx_cart -> _fx_fire).

Two tiers:
  - FxFiresStateSerializationTests: _fx_fires_state() itself, against a
    manually-populated _fx_fires dict -- no GStreamer, no DB.
  - FxFireLifecycleStateTests: the real _fx_fire()/_fx_stop() control
    flow (retrigger modes, polyphony cap, rejection paths), with
    Gst.ElementFactory.make patched to hand back plain MagicMocks so
    the actual GStreamer pipeline is never touched -- this project's
    own established pattern (test_engine_eos_plausibility.py) for
    testing engine control flow independent of real audio hardware/
    pipeline state. GStreamer's own real timing/audio behavior has its
    own dedicated coverage elsewhere (e.g.
    test_remote_dj_monitor_mixer_timeline.py); that's explicitly out
    of scope here per this task's own boundary (no GStreamer FX
    routing changes)."""
import json
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase

import library.services.engine as eng_module
from library.models import FXBusConfig, FXCart

# engine.py only calls Gst.init() from inside its own start()/run()
# method, not at import time -- FxFireLifecycleStateTests calls the
# real _fx_fire()/_fx_stop() control flow (with ElementFactory.make
# itself mocked, see _patched_element_factory), which still touches a
# few genuine GStreamer API calls (Gst.Caps.from_string) that require
# init() to have run first. Idempotent, safe to call here directly --
# same pattern test_remote_dj_monitor_mixer_timeline.py already uses.
eng_module.Gst.init(None)


def make_stand_in():
    """Bare PlaybackEngine instance, bypassing __init__ -- same
    technique as test_engine_eos_plausibility.py's make_stand_in.
    main_pipeline/fx_submix are MagicMocks: _fx_fire's own pipeline-
    building calls (add/get_clock/get_bus/request_pad_simple/...) all
    become harmless mock calls, so what's actually under test is the
    Python control flow around _fx_fires, not GStreamer itself."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj._fx_fires = {}
    obj._fx_lock = threading.Lock()
    obj._fx_next_id = 1
    obj.main_pipeline = MagicMock()
    obj.fx_submix = MagicMock()
    obj.pipeline_sample_rate = 44100
    return obj


def make_cart(**overrides):
    defaults = dict(name="Test Cart", gain_db=0.0, retrigger_mode="restart", enabled=True)
    defaults.update(overrides)
    if "filepath" not in defaults:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(b"\x00" * 16)
        tmp.close()
        defaults["filepath"] = tmp.name
    return FXCart.objects.create(**defaults)


class FxFiresStateSerializationTests(TransactionTestCase):
    def setUp(self):
        self.stand_in = make_stand_in()

    def test_no_active_fx_returns_empty_list(self):
        self.assertEqual(self.stand_in._fx_fires_state(), [])

    def test_active_fire_represented_with_cart_id_and_elapsed(self):
        self.stand_in._fx_fires[1] = {"cart_id": 42, "started_at": time.time() - 3.0}
        result = self.stand_in._fx_fires_state()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["cart_id"], 42)
        self.assertAlmostEqual(result[0]["elapsed_seconds"], 3.0, delta=0.2)
        self.assertEqual(set(result[0].keys()), {"cart_id", "elapsed_seconds"})

    def test_elapsed_seconds_computed_at_call_time_not_stored(self):
        self.stand_in._fx_fires[1] = {"cart_id": 7, "started_at": time.time() - 1.0}
        first = self.stand_in._fx_fires_state()[0]["elapsed_seconds"]
        # Backdate further and call again -- elapsed must move, proving
        # it's recomputed fresh from started_at each call, not cached.
        self.stand_in._fx_fires[1]["started_at"] -= 5.0
        second = self.stand_in._fx_fires_state()[0]["elapsed_seconds"]
        self.assertGreater(second, first + 4.0)

    def test_elapsed_seconds_never_negative(self):
        # Clock skew / started_at recorded a hair in the future --
        # must clamp, not report a negative elapsed time.
        self.stand_in._fx_fires[1] = {"cart_id": 5, "started_at": time.time() + 10.0}
        result = self.stand_in._fx_fires_state()
        self.assertGreaterEqual(result[0]["elapsed_seconds"], 0.0)

    def test_vt_fires_excluded_no_cart_id(self):
        # _vt_fire_file's fires always carry cart_id=None -- no
        # dashboard button to match, must never appear.
        self.stand_in._fx_fires[1] = {"cart_id": None, "vt_kind": "outro", "started_at": time.time()}
        self.assertEqual(self.stand_in._fx_fires_state(), [])

    def test_mixed_cart_and_vt_fires_only_cart_ones_returned(self):
        self.stand_in._fx_fires[1] = {"cart_id": 3, "started_at": time.time()}
        self.stand_in._fx_fires[2] = {"cart_id": None, "vt_kind": "intro", "started_at": time.time()}
        result = self.stand_in._fx_fires_state()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["cart_id"], 3)

    def test_multiple_different_carts_all_represented(self):
        self.stand_in._fx_fires[1] = {"cart_id": 1, "started_at": time.time()}
        self.stand_in._fx_fires[2] = {"cart_id": 2, "started_at": time.time()}
        result = self.stand_in._fx_fires_state()
        self.assertEqual({r["cart_id"] for r in result}, {1, 2})

    def test_does_not_collapse_duplicate_cart_ids_at_the_wire_layer(self):
        # _fx_fire's own retrigger handling makes this impossible via
        # the real firing path (verified separately below), but the
        # serialization function itself must not assume/enforce
        # uniqueness -- that's a UI-layer (one button per cart)
        # reduction, not a wire-format guarantee.
        self.stand_in._fx_fires[1] = {"cart_id": 9, "started_at": time.time() - 1.0}
        self.stand_in._fx_fires[2] = {"cart_id": 9, "started_at": time.time() - 2.0}
        result = self.stand_in._fx_fires_state()
        self.assertEqual(len(result), 2)
        self.assertEqual([r["cart_id"] for r in result], [9, 9])

    def test_output_is_json_serializable(self):
        self.stand_in._fx_fires[1] = {"cart_id": 11, "started_at": time.time()}
        json.dumps(self.stand_in._fx_fires_state())  # must not raise

    def test_does_not_mutate_underlying_fx_fires(self):
        self.stand_in._fx_fires[1] = {"cart_id": 4, "started_at": time.time()}
        before = dict(self.stand_in._fx_fires)
        self.stand_in._fx_fires_state()
        self.assertEqual(self.stand_in._fx_fires, before)


def _patched_element_factory():
    """Gst.ElementFactory.make -> a fresh MagicMock per call, so
    _fx_fire's pipeline-construction code runs for real (branch
    coverage on the actual Python control flow) without touching a
    real GStreamer pipeline."""
    return patch.object(eng_module.Gst.ElementFactory, "make", side_effect=lambda *a, **k: MagicMock())


class FxFireLifecycleStateTests(TransactionTestCase):
    def setUp(self):
        self.stand_in = make_stand_in()
        FXBusConfig.load()  # default polyphony_cap=4 unless a test overrides it

    def test_successful_fire_appears_in_state(self):
        cart = make_cart()
        with _patched_element_factory():
            started = self.stand_in._fx_fire(cart.id)
        self.assertTrue(started)
        state = self.stand_in._fx_fires_state()
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]["cart_id"], cart.id)

    def test_completed_fire_removed_from_state(self):
        cart = make_cart()
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
        (fire_id,) = list(self.stand_in._fx_fires.keys())
        self.stand_in._fx_stop(fire_id)  # simulates the EOS teardown path
        self.assertEqual(self.stand_in._fx_fires_state(), [])

    def test_disabled_cart_rejected_never_appears_active(self):
        cart = make_cart(enabled=False)
        with _patched_element_factory():
            started = self.stand_in._fx_fire(cart.id)
        self.assertFalse(started)
        self.assertEqual(self.stand_in._fx_fires_state(), [])

    def test_missing_file_rejected_never_appears_active(self):
        cart = FXCart.objects.create(name="Ghost", filepath="/nonexistent/path.wav", enabled=True)
        with _patched_element_factory():
            started = self.stand_in._fx_fire(cart.id)
        self.assertFalse(started)
        self.assertEqual(self.stand_in._fx_fires_state(), [])

    def test_nonexistent_cart_id_rejected_never_appears_active(self):
        with _patched_element_factory():
            started = self.stand_in._fx_fire(999999)
        self.assertFalse(started)
        self.assertEqual(self.stand_in._fx_fires_state(), [])

    def test_polyphony_cap_rejected_fire_never_appears_active(self):
        FXBusConfig.objects.filter(pk=1).update(polyphony_cap=1)
        cart_a = make_cart(name="A", retrigger_mode="restart")
        cart_b = make_cart(name="B", retrigger_mode="restart")
        with _patched_element_factory():
            self.assertTrue(self.stand_in._fx_fire(cart_a.id))
            started_b = self.stand_in._fx_fire(cart_b.id)
        self.assertFalse(started_b)
        state = self.stand_in._fx_fires_state()
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]["cart_id"], cart_a.id)  # only the one that actually got in

    def test_retrigger_restart_replaces_not_duplicates(self):
        cart = make_cart(retrigger_mode="restart")
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
            first_fire_id = next(iter(self.stand_in._fx_fires))
            started_again = self.stand_in._fx_fire(cart.id)
        self.assertTrue(started_again)
        state = self.stand_in._fx_fires_state()
        # Exactly one entry for this cart -- not two -- and it's a
        # fresh fire_id, not the original (the old one was torn down).
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]["cart_id"], cart.id)
        self.assertNotIn(first_fire_id, self.stand_in._fx_fires)

    def test_retrigger_ignore_drops_second_click_state_unchanged(self):
        cart = make_cart(retrigger_mode="ignore")
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
            before = dict(self.stand_in._fx_fires)
            started_again = self.stand_in._fx_fire(cart.id)
        self.assertFalse(started_again)
        self.assertEqual(self.stand_in._fx_fires, before)
        state = self.stand_in._fx_fires_state()
        self.assertEqual(len(state), 1)  # still just the original, not duplicated

    def test_retrigger_stop_halts_and_reports_not_playing(self):
        cart = make_cart(retrigger_mode="stop")
        with _patched_element_factory():
            self.stand_in._fx_fire(cart.id)
            self.assertEqual(len(self.stand_in._fx_fires_state()), 1)
            started_again = self.stand_in._fx_fire(cart.id)
        self.assertFalse(started_again)
        # Second click on 'stop' mode halts playback entirely -- state
        # must show nothing active for this cart, not the old fire
        # lingering as "still playing".
        self.assertEqual(self.stand_in._fx_fires_state(), [])

    def test_different_carts_do_not_interfere_with_each_others_state(self):
        cart_a = make_cart(name="A")
        cart_b = make_cart(name="B")
        with _patched_element_factory():
            self.stand_in._fx_fire(cart_a.id)
            self.stand_in._fx_fire(cart_b.id)
        state = self.stand_in._fx_fires_state()
        self.assertEqual({s["cart_id"] for s in state}, {cart_a.id, cart_b.id})
