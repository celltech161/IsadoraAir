"""P1 1.5 Pass B1 -- bounded disconnect/recovery + Auto/Manual ownership
closeout.

Exercises _remote_dj_on_connection_state's product-level Reconnecting
state machine, the bounded engine-side recovery deadline
(_remote_dj_on_recovery_deadline), and _remote_dj_session_stop's final
Auto/Manual ownership-outcome evidence, plus a lightweight text-contract
check on the corresponding dashboard.html browser-side change (same
pattern test_remote_dj_transport_observability.py already established
for its own JS contract).

Django TestCase (not SimpleTestCase): the code under test reads
RemoteDJConfig.load() and calls monitoring.models.emit_event, both of
which touch the real database.

GLib.timeout_add is patched to CAPTURE (interval_ms, callback, context)
rather than really scheduling anything -- the captured callback is
invoked directly to deterministically simulate "grace expired" with no
real wall-clock wait and no dependency on a running GLib main loop.
"""
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstWebRTC

from django.core.exceptions import ValidationError
from django.test import TestCase

import library.services.engine as eng_module
from library.models import RemoteDJConfig
from library.services.remote_dj_connection import RemoteDJConnectionAttempt
from monitoring.models import SystemEvent

ATTEMPT_A = "B1_attempt_generation_A0000000"
ATTEMPT_B = "B1_attempt_generation_B0000000"


class RemoteDJReconnectRecoveryTests(TestCase):
    def setUp(self):
        Gst.init(None)
        self.engine = object.__new__(eng_module.PlaybackEngine)
        self.engine._remote_dj_server = MagicMock()
        self.engine.manual_mode = False
        self.engine._manual_from_mic = False
        self.engine.mic_live = False
        self.engine._manual_hold_pending = False
        self.engine._next_triggered = False
        self.engine.remote_dj_tee = MagicMock()
        self.engine.local_mic_tee = None
        self.engine._remote_dj_last_attempt = None

        talk_ducking_patcher = patch.object(self.engine, "_apply_talk_ducking")
        talk_ducking_patcher.start()
        self.addCleanup(talk_ducking_patcher.stop)

        # A real "volume" element (not a MagicMock) so get_property("volume")
        # reflects real set_property calls -- the mute/restore logic under
        # test branches on the CURRENT value, which a bare MagicMock can't
        # model (comparing a MagicMock to a float raises).
        remote_gate = Gst.ElementFactory.make("volume", None)
        remote_gate.set_property("volume", 0.0)
        self.slot = eng_module.RemoteDJSlot(
            slot_id=0, selector=MagicMock(), silence_pad=MagicMock(),
            webrtc_pad=MagicMock(), remote_gain=MagicMock(),
            remote_gate=remote_gate, master_mixer_pad=MagicMock(),
            silence_src=MagicMock(),
        )
        self.engine.dj_slots = [self.slot]

        self.attempt_id = ATTEMPT_A
        self.session = eng_module.RemoteDJSession()
        self.session.connection_attempt = RemoteDJConnectionAttempt(
            self.attempt_id, int(time.time() * 1000)
        )
        self.session.webrtc = MagicMock()
        self.session.slot_id = 0
        self.slot.session = self.session
        self.engine.remote_dj_session = self.session

        # Only "connected" -> DISCONNECTED starts the recovery grace (see
        # RemoteDJConnectionAttempt.mark_reconnecting) -- establish that
        # baseline exactly like a real successful connect would.
        self.session.connection_attempt.record("peer_connected")
        self.assertEqual(self.session.connection_attempt.status, "connected")

        cfg = RemoteDJConfig.load()
        cfg.enabled = True
        cfg.reconnect_grace_seconds = 10
        cfg.save()

        self._captured_timers = []

        def _fake_timeout_add(interval_ms, callback, context):
            self._captured_timers.append((interval_ms, callback, context))
            return 4242

        timeout_add_patcher = patch.object(
            eng_module.GLib, "timeout_add", side_effect=_fake_timeout_add
        )
        timeout_add_patcher.start()
        self.addCleanup(timeout_add_patcher.stop)

    def _fire(self, session, attempt_id, state):
        session.webrtc.props.connection_state = state
        self.engine._remote_dj_on_connection_state(
            session.webrtc, None, (session, attempt_id, session.webrtc)
        )

    def _gate_volume(self):
        return self.slot.remote_gate.get_property("volume")

    # -- 1/2/3: entering Reconnecting, immediate physical mute, gate_desired kept --

    def test_recoverable_disconnect_enters_reconnecting(self):
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self.assertEqual(self.session.connection_attempt.status, "reconnecting")
        self.assertEqual(len(self._captured_timers), 1)
        interval_ms, _callback, _context = self._captured_timers[0]
        self.assertEqual(interval_ms, 10000)

    def test_physical_gate_forced_off_immediately(self):
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self.assertEqual(self._gate_volume(), 0.0)

    def test_desired_gate_state_is_retained_through_the_mute(self):
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self.assertTrue(self.session.gate_desired)

    # -- 4/5/6: same-session recovery --

    def test_same_session_recovery_within_grace_cancels_timeout(self):
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.CONNECTED)
        self.assertEqual(self.session.connection_attempt.status, "connected")
        self.assertEqual(self.session.recovery_deadline_source_id, 0)

    def test_gate_restores_on_recovery_only_when_desired(self):
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.CONNECTED)
        self.assertEqual(self._gate_volume(), 1.0)

    def test_recovery_with_gate_not_desired_remains_muted(self):
        self.session.gate_desired = False  # never opened
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.CONNECTED)
        self.assertEqual(self._gate_volume(), 0.0)

    def test_recovered_session_is_not_a_new_session(self):
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.CONNECTED)
        self.assertIs(self.engine.remote_dj_session, self.session)
        self.assertIs(self.slot.session, self.session)

    def test_a_stale_recovery_timer_after_genuine_recovery_does_not_finalize(self):
        """Belt-and-braces: even if the deadline callback somehow still
        fired after a genuine recovery (GLib.source_remove is called, but
        this proves the callback's OWN status check is also sufficient)."""
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.CONNECTED)
        _interval_ms, callback, context = self._captured_timers[0]
        callback(context)
        self.assertIs(self.engine.remote_dj_session, self.session)
        self.assertEqual(self.session.connection_attempt.status, "connected")

    # -- 7: grace expiry finalizes and releases the slot --

    def test_grace_expiry_finalizes_and_releases_the_slot(self):
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        _interval_ms, callback, context = self._captured_timers[0]
        callback(context)
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertIsNone(self.slot.session)
        self.assertEqual(self._gate_volume(), 0.0)
        self.assertEqual(self.session.connection_attempt.status, "failed")

    def test_grace_expiry_emits_bounded_telemetry(self):
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        _interval_ms, callback, context = self._captured_timers[0]
        callback(context)
        self.assertTrue(
            SystemEvent.objects.filter(
                dedupe_key=f"engine|remote_dj|grace_expired|attempt={self.attempt_id}"
            ).exists()
        )

    # -- 8/9/10: terminal states finalize promptly / immediately --

    def test_terminal_failed_finalizes_without_waiting_for_grace(self):
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.FAILED)
        self.assertIsNone(self.engine.remote_dj_session)
        # No recovery grace was ever scheduled for a direct terminal FAILED.
        self.assertEqual(self._captured_timers, [])

    def test_terminal_closed_finalizes(self):
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.CLOSED)
        self.assertIsNone(self.engine.remote_dj_session)

    def test_explicit_disconnect_is_immediate_and_final(self):
        self.engine._remote_dj_session_stop()
        self.assertIsNone(self.engine.remote_dj_session)

    # -- 11/12: stale generation safety --

    def test_stale_recovery_timer_from_attempt_a_cannot_finalize_attempt_b(self):
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        _interval_ms, callback, context = self._captured_timers[0]

        # A is torn down through an unrelated path (e.g. an explicit
        # operator disconnect), then B claims the same (only) slot.
        self.engine._remote_dj_session_stop()
        self.assertIsNone(self.engine.remote_dj_session)

        session_b = eng_module.RemoteDJSession()
        session_b.connection_attempt = RemoteDJConnectionAttempt(
            ATTEMPT_B, int(time.time() * 1000)
        )
        session_b.webrtc = MagicMock()
        session_b.slot_id = 0
        session_b.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self.slot.session = session_b
        self.engine.remote_dj_session = session_b

        callback(context)  # A's stale grace-expiry timer fires late

        self.assertIs(self.engine.remote_dj_session, session_b)
        self.assertIs(self.slot.session, session_b)
        self.assertEqual(self._gate_volume(), 1.0)

    def test_stale_connected_notify_from_attempt_a_cannot_mutate_attempt_b(self):
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)

        session_b = eng_module.RemoteDJSession()
        session_b.connection_attempt = RemoteDJConnectionAttempt(
            ATTEMPT_B, int(time.time() * 1000)
        )
        session_b.webrtc = MagicMock()
        session_b.slot_id = 0
        session_b.gate_desired = False
        self.slot.session = session_b
        self.engine.remote_dj_session = session_b

        # A's own webrtcbin fires a (now stale) CONNECTED notify.
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.CONNECTED)

        self.assertIs(self.engine.remote_dj_session, session_b)
        self.assertEqual(self._gate_volume(), 0.0)  # untouched by A's stale notify

    # -- 13/14: post-finalization callbacks are harmless --

    def test_native_ice_failure_after_finalization_is_harmless(self):
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        _interval_ms, callback, context = self._captured_timers[0]
        callback(context)
        self.assertIsNone(self.engine.remote_dj_session)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.FAILED)  # must not raise
        self.assertIsNone(self.engine.remote_dj_session)

    def test_signaling_disconnect_after_finalization_is_harmless(self):
        self.engine._remote_dj_session_stop()
        result = self.engine._remote_dj_session_stop(self.attempt_id)  # must not raise
        self.assertFalse(result)

    # -- 15-18: Auto/Manual ownership outcomes --

    def test_case_a_remote_caused_auto_to_manual_then_final_loss_restores_auto(self):
        self.engine.manual_mode = True
        self.engine._manual_from_mic = True
        self.engine.mic_live = False
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        with patch.object(eng_module, "emit_event") as mock_emit:
            self.engine._remote_dj_session_stop()
        self.assertFalse(self.engine.manual_mode)
        outcomes = [
            c for c in mock_emit.call_args_list
            if c.kwargs.get("dedupe_key", "").startswith("engine|remote_dj|automation_outcome")
        ]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].kwargs["detail"]["reason"], "remote_mic_auto_hold_released")
        self.assertEqual(outcomes[0].kwargs["detail"]["automation_restored"], "auto")

    def test_case_b_pre_existing_operator_manual_survives_remote_loss(self):
        self.engine.manual_mode = True
        self.engine._manual_from_mic = False  # operator set this before Remote DJ connected
        self.engine.mic_live = False
        with patch.object(eng_module, "emit_event") as mock_emit:
            self.engine._remote_dj_session_stop()
        self.assertTrue(self.engine.manual_mode)
        outcomes = [
            c for c in mock_emit.call_args_list
            if c.kwargs.get("dedupe_key", "").startswith("engine|remote_dj|automation_outcome")
        ]
        self.assertEqual(outcomes[0].kwargs["detail"]["reason"], "operator_manual")
        self.assertEqual(outcomes[0].kwargs["detail"]["automation_preserved"], "manual")

    def test_case_c_operator_asserted_manual_during_session_survives_loss(self):
        # Same mechanism as Case B (_manual_from_mic is False either way)
        # -- an operator toggle during the session clears
        # _manual_from_mic exactly like one before it (see
        # PlaybackEngine._set_manual_mode).
        self.engine.manual_mode = True
        self.engine._manual_from_mic = False
        self.engine.mic_live = False
        self.engine._remote_dj_session_stop()
        self.assertTrue(self.engine.manual_mode)

    def test_case_d_studio_mic_overlap_preserves_manual(self):
        self.engine.manual_mode = True
        self.engine._manual_from_mic = True
        self.engine.mic_live = True  # Studio Mic still legitimately live
        with patch.object(eng_module, "emit_event") as mock_emit:
            self.engine._remote_dj_session_stop()
        self.assertTrue(self.engine.manual_mode)
        outcomes = [
            c for c in mock_emit.call_args_list
            if c.kwargs.get("dedupe_key", "").startswith("engine|remote_dj|automation_outcome")
        ]
        self.assertEqual(outcomes[0].kwargs["detail"]["reason"], "studio_mic_still_live")
        self.assertEqual(outcomes[0].kwargs["detail"]["automation_preserved"], "manual")

    def test_no_ownership_evidence_when_never_manual(self):
        self.engine.manual_mode = False
        self.engine._manual_from_mic = False
        with patch.object(eng_module, "emit_event") as mock_emit:
            self.engine._remote_dj_session_stop()
        outcomes = [
            c for c in mock_emit.call_args_list
            if c.kwargs.get("dedupe_key", "").startswith("engine|remote_dj|automation_outcome")
        ]
        self.assertEqual(outcomes, [])

    # -- 19: no stale PTT/audio survives finalization --

    def test_no_stale_gate_audio_survives_finalization(self):
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        _interval_ms, callback, context = self._captured_timers[0]
        callback(context)
        self.assertEqual(self._gate_volume(), 0.0)
        # A brand-new session for the next connect starts with a fresh,
        # unmuted-intent-free gate_desired -- nothing carries over.
        fresh = eng_module.RemoteDJSession()
        self.assertFalse(fresh.gate_desired)

    # -- 22: the slot frees up on the product deadline, not the ~49s native one --

    def test_slot_available_again_after_product_grace_not_native_49s_wait(self):
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self.assertIsNotNone(self.engine.remote_dj_session)  # still occupied during grace
        _interval_ms, callback, context = self._captured_timers[0]
        callback(context)  # simulated 10s grace elapsing, NOT a real 49s wait
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertIsNone(self.slot.session)

    # -- 24: RemoteDJConfig.reconnect_grace_seconds validation --

    def test_reconnect_grace_seconds_default_is_ten(self):
        cfg = RemoteDJConfig()
        self.assertEqual(cfg.reconnect_grace_seconds, 10)

    def test_reconnect_grace_seconds_rejects_out_of_bounds_low(self):
        cfg = RemoteDJConfig.load()
        cfg.reconnect_grace_seconds = 1
        with self.assertRaises(ValidationError):
            cfg.full_clean()

    def test_reconnect_grace_seconds_rejects_out_of_bounds_high(self):
        cfg = RemoteDJConfig.load()
        cfg.reconnect_grace_seconds = 60
        with self.assertRaises(ValidationError):
            cfg.full_clean()

    def test_reconnect_grace_seconds_accepts_bounds_inclusive(self):
        cfg = RemoteDJConfig.load()
        cfg.reconnect_grace_seconds = 3
        cfg.full_clean()  # must not raise
        cfg.reconnect_grace_seconds = 30
        cfg.full_clean()  # must not raise

    def test_reconnect_grace_seconds_is_read_fresh_not_cached(self):
        """Applies immediately -- no engine restart required. Changing
        the config between two disconnects uses the NEW value for the
        second one, since it's read fresh at the moment grace starts."""
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.CONNECTED)

        cfg = RemoteDJConfig.load()
        cfg.reconnect_grace_seconds = 20
        cfg.save()

        self._fire(self.session, self.attempt_id, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self.assertEqual(len(self._captured_timers), 2)
        self.assertEqual(self._captured_timers[1][0], 20000)


class RemoteDJReconnectDashboardTemplateContractTests(TestCase):
    """Lightweight text-contract check on the browser-side change --
    same pattern as test_remote_dj_transport_observability.py's own
    test_browser_template_uses_bounded_generation_safe_stats_burst.
    Does not execute JS; verifies the source no longer contains the
    eager-teardown-on-'disconnected' pattern and does contain the new
    Reconnecting-aware handling."""

    def setUp(self):
        self.template = (
            Path(__file__).parents[1] / "templates/library/dashboard.html"
        ).read_text(encoding="utf-8")

    def test_disconnected_no_longer_eagerly_tears_down_the_peer(self):
        self.assertNotIn(
            "['failed', 'disconnected', 'closed'].includes(statsPc.iceConnectionState)",
            self.template,
        )
        self.assertIn(
            "['failed', 'closed'].includes(statsPc.iceConnectionState)",
            self.template,
        )

    def test_reconnecting_flag_and_ui_text_present(self):
        self.assertIn("let rdjReconnecting = false;", self.template)
        self.assertIn("'Reconnecting…'", self.template)
        self.assertIn("'Remote reconnecting…'", self.template)

    def test_ws_onclose_finalizes_when_peer_still_open(self):
        self.assertIn("if (!rdjPc) { rdjConnectFail('Closed (' + e.code + ')'); return; }", self.template)
        self.assertIn("rdjReconnecting = false;\n    rdjDisconnect();\n  };", self.template)
