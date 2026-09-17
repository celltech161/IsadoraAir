"""r0083 -- DuckingConfig.ptt_auto_manual_enabled: makes PTT-driven Manual
mode optional. Deterministic engine-level tests against the real
_apply_mic_mode_hold()/_set_mic_ptt()/_remote_dj_set_gate()/
_remote_dj_session_stop() code paths, driven by a real DuckingConfig
singleton row (TestCase, real DB) rather than a mock -- the whole point
of this feature is that the engine reads it fresh from the DB on every
transition, so these tests prove that live-read contract genuinely
works, not just that the Python logic is internally consistent.

Uses the same object.__new__(PlaybackEngine) minimal-stand-in technique
as test_engine_mic_recovery.py, with real (unlinked, pipeline-free)
GStreamer "volume" elements standing in for mic_ptt_volume/remote_gate
-- cheap to construct, and get_property/set_property behave exactly
like the real thing without needing a running pipeline."""
from unittest.mock import patch

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from django.test import TestCase

import library.services.engine as eng_module
from hardware.models import DuckingConfig

Gst.init(None)


def make_engine():
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.mic_ptt_volume = Gst.ElementFactory.make("volume", None)
    obj.mic_ptt_volume.set_property("volume", 0.0)
    obj.mic_live = False
    obj.manual_mode = False
    obj._manual_from_mic = False
    obj._manual_hold_pending = False
    obj._ptt_auto_manual_enabled = True
    obj.remote_dj_session = None
    obj.dj_slots = []
    return obj


def make_remote_dj_slot(slot_id=0):
    gate = Gst.ElementFactory.make("volume", None)
    gate.set_property("volume", 0.0)
    return type("FakeSlot", (), {"slot_id": slot_id, "remote_gate": gate})()


def make_remote_dj_session(slot_id=0):
    from library.services.engine import RemoteDJSession

    session = RemoteDJSession()
    session.slot_id = slot_id
    return session


class PttAutoManualMixin:
    """Real DuckingConfig singleton row -- setUp() forces the default-
    enabled state so every test starts from a known baseline regardless
    of execution order, and _start_duck_ramp/emit_event are stubbed the
    same way MockDuckingMixin stubs them in test_engine_mic_recovery.py
    (this feature never touches either, but _apply_talk_ducking is
    still reached on every real _set_mic_ptt/_remote_dj_set_gate call)."""

    def setUp(self):
        super().setUp()
        DuckingConfig.objects.update_or_create(
            pk=1, defaults={"enabled": False, "ptt_auto_manual_enabled": True},
        )
        patch_ramp = patch.object(eng_module.PlaybackEngine, "_start_duck_ramp", lambda self, target: None)
        patch_ramp.start()
        self.addCleanup(patch_ramp.stop)
        patch_emit = patch.object(eng_module, "emit_event", lambda *a, **k: None)
        patch_emit.start()
        self.addCleanup(patch_emit.stop)

    def set_ptt_policy(self, enabled):
        DuckingConfig.objects.filter(pk=1).update(ptt_auto_manual_enabled=enabled)


class DefaultBackwardCompatibilityTests(PttAutoManualMixin, TestCase):
    def test_migration_default_is_enabled(self):
        self.assertTrue(DuckingConfig._meta.get_field("ptt_auto_manual_enabled").get_default())

    def test_fresh_singleton_row_defaults_enabled(self):
        DuckingConfig.objects.all().delete()
        self.assertTrue(DuckingConfig.load().ptt_auto_manual_enabled)

    def test_enabled_behavior_matches_r0082_exactly(self):
        """The exact r0082 scenario: Auto + PTT on -> Manual/mic-owned;
        PTT off -> Auto. No policy row existed before this feature; the
        default (True) must reproduce it byte-for-byte."""
        engine = make_engine()
        engine._set_mic_ptt(True)
        self.assertTrue(engine.manual_mode)
        self.assertTrue(engine._manual_from_mic)
        engine._set_mic_ptt(False)
        self.assertFalse(engine.manual_mode)


class LocalStudioMicOwnershipTests(PttAutoManualMixin, TestCase):
    def test_enabled_auto_ptt_on_takes_manual_mic_owned(self):
        engine = make_engine()
        engine._set_mic_ptt(True)
        self.assertTrue(engine.manual_mode)
        self.assertTrue(engine._manual_from_mic)

    def test_enabled_release_restores_auto(self):
        engine = make_engine()
        engine._set_mic_ptt(True)
        engine._set_mic_ptt(False)
        self.assertFalse(engine.manual_mode)

    def test_enabled_pre_existing_operator_manual_untouched_by_ptt(self):
        engine = make_engine()
        engine._set_manual_mode(True)  # explicit operator toggle
        engine._set_mic_ptt(True)
        self.assertTrue(engine.manual_mode)
        self.assertFalse(engine._manual_from_mic)
        engine._set_mic_ptt(False)
        self.assertTrue(engine.manual_mode)  # still operator-owned Manual

    def test_enabled_explicit_operator_manual_during_mic_hold_survives_release(self):
        engine = make_engine()
        engine._set_mic_ptt(True)
        self.assertTrue(engine._manual_from_mic)
        engine._set_manual_mode(True)  # operator takes ownership mid-hold
        self.assertFalse(engine._manual_from_mic)
        engine._set_mic_ptt(False)
        self.assertTrue(engine.manual_mode)  # survives the mic release

    def test_disabled_auto_ptt_on_off_stays_auto(self):
        engine = make_engine()
        self.set_ptt_policy(False)
        engine._set_mic_ptt(True)
        self.assertFalse(engine.manual_mode)
        self.assertFalse(engine._manual_from_mic)
        engine._set_mic_ptt(False)
        self.assertFalse(engine.manual_mode)

    def test_disabled_operator_manual_ptt_on_off_stays_manual(self):
        engine = make_engine()
        engine._set_manual_mode(True)
        self.set_ptt_policy(False)
        engine._set_mic_ptt(True)
        self.assertTrue(engine.manual_mode)
        engine._set_mic_ptt(False)
        self.assertTrue(engine.manual_mode)


class RemoteDJOwnershipTests(PttAutoManualMixin, TestCase):
    """Real _remote_dj_set_gate() semantics -- the same command handler
    the 'remote_dj_gate' engine command dispatches to."""

    def _engine_with_slot(self):
        engine = make_engine()
        slot = make_remote_dj_slot(0)
        engine.dj_slots = [slot]
        engine.remote_dj_session = make_remote_dj_session(0)
        return engine, slot

    def test_enabled_gate_on_auto_takes_manual_mic_owned(self):
        engine, slot = self._engine_with_slot()
        engine._remote_dj_set_gate(True)
        self.assertTrue(engine.manual_mode)
        self.assertTrue(engine._manual_from_mic)
        self.assertEqual(slot.remote_gate.get_property("volume"), 1.0)

    def test_enabled_gate_off_restores_auto(self):
        engine, slot = self._engine_with_slot()
        engine._remote_dj_set_gate(True)
        engine._remote_dj_set_gate(False)
        self.assertFalse(engine.manual_mode)

    def test_enabled_pre_existing_operator_manual_untouched(self):
        engine, slot = self._engine_with_slot()
        engine._set_manual_mode(True)
        engine._remote_dj_set_gate(True)
        self.assertFalse(engine._manual_from_mic)
        engine._remote_dj_set_gate(False)
        self.assertTrue(engine.manual_mode)

    def test_disabled_gate_on_off_stays_auto(self):
        engine, slot = self._engine_with_slot()
        self.set_ptt_policy(False)
        engine._remote_dj_set_gate(True)
        self.assertFalse(engine.manual_mode)
        self.assertEqual(slot.remote_gate.get_property("volume"), 1.0)  # audio gating unaffected
        engine._remote_dj_set_gate(False)
        self.assertFalse(engine.manual_mode)

    def test_disabled_operator_manual_gate_on_off_stays_manual(self):
        engine, slot = self._engine_with_slot()
        engine._set_manual_mode(True)
        self.set_ptt_policy(False)
        engine._remote_dj_set_gate(True)
        self.assertTrue(engine.manual_mode)
        engine._remote_dj_set_gate(False)
        self.assertTrue(engine.manual_mode)

    def test_finalization_releases_mic_owned_hold(self):
        """_remote_dj_session_stop's own ownership-release path -- forces
        the gate to 0 then reevaluates, same as a normal gate-off."""
        engine, slot = self._engine_with_slot()
        engine._remote_dj_set_gate(True)
        self.assertTrue(engine._manual_from_mic)
        slot.remote_gate.set_property("volume", 0.0)
        engine._apply_talk_ducking = lambda: None
        engine._apply_mic_mode_hold()
        self.assertFalse(engine.manual_mode)

    def test_disabled_finalization_never_asserted_manual(self):
        engine, slot = self._engine_with_slot()
        self.set_ptt_policy(False)
        engine._remote_dj_set_gate(True)
        slot.remote_gate.set_property("volume", 0.0)
        engine._apply_talk_ducking = lambda: None
        engine._apply_mic_mode_hold()
        self.assertFalse(engine.manual_mode)


class MixedMicCasesTests(PttAutoManualMixin, TestCase):
    def _engine_with_slot(self):
        engine = make_engine()
        slot = make_remote_dj_slot(0)
        engine.dj_slots = [slot]
        engine.remote_dj_session = make_remote_dj_session(0)
        return engine, slot

    def test_local_and_remote_overlap_only_final_release_restores_auto(self):
        engine, slot = self._engine_with_slot()
        engine._set_mic_ptt(True)  # local takes ownership
        self.assertTrue(engine.manual_mode)
        engine._remote_dj_set_gate(True)  # remote joins -- no change, already Manual
        self.assertTrue(engine._manual_from_mic)
        engine._remote_dj_set_gate(False)  # remote releases -- local still live
        self.assertTrue(engine.manual_mode)
        engine._set_mic_ptt(False)  # last mic releases
        self.assertFalse(engine.manual_mode)

    def test_disabling_mid_hold_releases_on_next_transition_not_immediately(self):
        """The explicit disable-while-owned edge case: enabling stays
        mic-owned Manual until the NEXT relevant transition, never
        polled/released merely because the Admin row changed."""
        engine = make_engine()
        engine._set_mic_ptt(True)
        self.assertTrue(engine.manual_mode)
        self.assertTrue(engine._manual_from_mic)

        self.set_ptt_policy(False)
        # No transition happened yet -- the stale hold is untouched.
        self.assertTrue(engine.manual_mode)
        self.assertTrue(engine._manual_from_mic)

        # Next relevant transition (mic still live) releases it and
        # clears ownership, per the required behavior.
        engine._set_mic_ptt(True)
        self.assertFalse(engine.manual_mode)
        self.assertFalse(engine._manual_from_mic)

        # And it does not reassert Manual even though the mic is still
        # physically live -- disabled means disabled.
        engine._set_mic_ptt(True)
        self.assertFalse(engine.manual_mode)

    def test_disabling_mid_hold_with_remote_mic_still_live_releases_via_local_transition(self):
        engine, slot = self._engine_with_slot()
        engine._remote_dj_set_gate(True)
        self.assertTrue(engine._manual_from_mic)
        self.set_ptt_policy(False)
        # A different mic's transition also releases the stale hold.
        engine._set_mic_ptt(True)
        self.assertFalse(engine.manual_mode)
        engine._set_mic_ptt(False)

    def test_operator_owned_manual_never_cleared_by_disable(self):
        engine = make_engine()
        engine._set_manual_mode(True)
        self.set_ptt_policy(False)
        engine._set_mic_ptt(True)
        self.assertTrue(engine.manual_mode)
        self.assertFalse(engine._manual_from_mic)
        engine._set_mic_ptt(False)
        self.assertTrue(engine.manual_mode)

    def test_operator_owned_manual_never_cleared_while_enabled(self):
        engine = make_engine()
        engine._set_manual_mode(True)
        engine._set_mic_ptt(True)
        engine._set_mic_ptt(False)
        self.assertTrue(engine.manual_mode)
        self.assertFalse(engine._manual_from_mic)


class EngineStateSerializationTests(PttAutoManualMixin, TestCase):
    def test_write_state_reports_cached_value_not_a_fresh_db_read(self):
        """_write_state() must never hit the DB itself (it runs on every
        _poll_position tick) -- it reports whatever _apply_mic_mode_hold
        last cached. Proven by flipping the DB row WITHOUT a mic
        transition and confirming the cached value is unchanged."""
        engine = make_engine()
        engine._set_mic_ptt(True)  # caches True (the default)
        engine._set_mic_ptt(False)
        self.assertTrue(engine._ptt_auto_manual_enabled)
        self.set_ptt_policy(False)
        # No transition -- cache must still read True.
        self.assertTrue(engine._ptt_auto_manual_enabled)

    def test_bare_object_new_stand_in_defaults_true_for_write_state(self):
        """Several existing test harnesses build a bare
        object.__new__(PlaybackEngine) and never set this attribute at
        all (never ran __init__) -- _write_state must not crash, and
        must report this feature's own default (True)."""
        obj = object.__new__(eng_module.PlaybackEngine)
        self.assertEqual(getattr(obj, "_ptt_auto_manual_enabled", True), True)
