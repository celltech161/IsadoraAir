"""r0132 Package B -- Remote DJ failure teardown reliability (WRJE 2026-11-08).

webrtcbin delivers notify::connection-state (and GstPromise replies) on its
own "<name>:pc" thread. r0131 tore the session down right there, calling
set_state(NULL) on that same webrtcbin, whose READY->NULL _quit_pc_loop
waits for the loop it is running inside: the call never returns, the slot is
never released, and the engine's own shutdown NULL then waits on that
element's state lock. These tests drive the real lifecycle methods with a
real GStreamer pipeline and real elements; a "stuck" NULL is simulated by
overriding one element's set_state so nothing native ever hangs.
"""
import threading
import time
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstWebRTC  # noqa: E402

from django.db import connections  # noqa: E402
from django.test import TestCase  # noqa: E402

from library.services import engine as eng  # noqa: E402
from library.services.remote_dj_connection import RemoteDJConnectionAttempt  # noqa: E402
from monitoring.models import SystemEvent  # noqa: E402

ATTEMPT_A = "pinn7PDv13zCxC2GSvvR90Ft"
ATTEMPT_B = "Bq8sYx2LkMfT4rWvNz6pHc1D"
FAILED = GstWebRTC.WebRTCPeerConnectionState.FAILED
CLOSED = GstWebRTC.WebRTCPeerConnectionState.CLOSED


class RemoteDJTeardownTests(TestCase):
    def setUp(self):
        Gst.init(None)
        self.enterContext(patch.object(eng, "DJ_DIAG_LOG", eng.Path(self._tmpdir()) / "diag.log"))
        self.enterContext(patch.object(eng, "DJ_DUMP_PCM", eng.Path(self._tmpdir()) / "dump.pcm"))
        self.idle = []
        self.timers = []
        self.enterContext(patch.object(eng.GLib, "idle_add", side_effect=lambda fn, *a: self.idle.append((fn, a)) or 1))
        self.enterContext(patch.object(
            eng.GLib, "timeout_add", side_effect=lambda ms, fn, *a: self.timers.append((ms, fn, a)) or 2))
        self.enterContext(patch.object(eng, "REMOTE_DJ_TEARDOWN_TIMEOUT_SECONDS", 0.5, create=True))

        engine = object.__new__(eng.PlaybackEngine)
        engine._remote_dj_server = MagicMock()
        engine.manual_mode = False
        engine._manual_from_mic = False
        engine.mic_live = False
        engine._manual_hold_pending = False
        engine._next_triggered = False
        engine.remote_dj_tee = MagicMock()
        engine.local_mic_tee = None
        engine._remote_dj_last_attempt = None
        engine.remote_dj_session = None
        engine._apply_talk_ducking = MagicMock()
        self.hold_threads = []
        engine._apply_mic_mode_hold = lambda: self.hold_threads.append(threading.current_thread())
        engine.main_pipeline = Gst.Pipeline.new("r0132b-main")
        self.addCleanup(engine.main_pipeline.set_state, Gst.State.NULL)
        self.engine = engine

        gate = Gst.ElementFactory.make("volume", None)
        gate.set_property("volume", 0.0)
        webrtc_pad = MagicMock()
        webrtc_pad.get_peer.return_value = None
        self.slot = eng.RemoteDJSlot(
            slot_id=0, selector=MagicMock(), silence_pad=object(), webrtc_pad=webrtc_pad,
            remote_gain=MagicMock(), remote_gate=gate, master_mixer_pad=object(), silence_src=object(),
        )
        engine.dj_slots = [self.slot]
        self.release = threading.Event()
        self.addCleanup(self.release.set)            # never leave a "stuck" worker behind

    def _tmpdir(self):
        import tempfile
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name

    # -- harness ---------------------------------------------------------

    def session(self, attempt_id, *, live=True, stuck=False, null_fault=None):
        """A live session occupying the slot with two real ephemeral elements
        in main_pipeline (PLAYING). Every set_state call records its thread.
        null_fault ("raise" | "failure") makes the first element's NULL raise
        or return Gst.StateChangeReturn.FAILURE without changing state; the
        real NULL then runs at test cleanup so nothing is left PLAYING."""
        session = eng.RemoteDJSession()
        session.connection_attempt = RemoteDJConnectionAttempt(attempt_id, int(time.time() * 1000))
        session.connection_attempt.record("peer_connected")
        session.webrtc = MagicMock()
        session.slot_id = 0
        session.null_threads = []
        for index in range(2):
            element = Gst.ElementFactory.make("fakesrc" if index == 0 else "fakesink", None)
            self.engine.main_pipeline.add(element)
            element.sync_state_with_parent()
            real = Gst.Element.set_state

            fault = null_fault if index == 0 else None

            def recording(state, element=element, block=(stuck and index == 0), fault=fault):
                session.null_threads.append(threading.current_thread())
                if block:
                    self.release.wait(30)
                if fault == "raise":
                    raise RuntimeError("simulated native teardown error")
                if fault == "failure":
                    return Gst.StateChangeReturn.FAILURE
                return real(element, state)
            element.set_state = recording
            if fault:
                self.addCleanup(real, element, Gst.State.NULL)
            session.elements.append(element)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.slot.session = session
        self.engine.remote_dj_session = session
        if live:
            self.slot.remote_gate.set_property("volume", 1.0)
            session.gate_desired = True
        return session

    def notify(self, session, attempt_id, state, *, on_pc_thread=True):
        session.webrtc.props.connection_state = state
        call = lambda: self.engine._remote_dj_on_connection_state(  # noqa: E731
            session.webrtc, None, (session, attempt_id, session.webrtc))
        if not on_pc_thread:
            return call()

        def pc_thread():
            try:
                call()          # r0131's own per-edge info event opens a DB connection here
            finally:
                connections.close_all()
        thread = threading.Thread(target=pc_thread, name="webrtcbin0:pc")
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the notify callback thread must return promptly")
        return thread

    def run_idle(self):
        while self.idle:
            fn, args = self.idle.pop(0)
            fn(*args)

    def finish_cleanup(self, limit=5.0):
        """Drive the scheduled GLib poll until it stops (as the main loop would)."""
        deadline = time.monotonic() + limit
        while self.timers:
            _ms, fn, args = self.timers[0]
            if not fn(*args):
                self.timers.pop(0)
                continue
            self.assertLess(time.monotonic(), deadline, "teardown poll never resolved")
            time.sleep(0.02)

    def gate(self):
        return self.slot.remote_gate.get_property("volume")

    # -- 1. terminal FAILED ----------------------------------------------

    def test_failed_on_the_pc_thread_mutes_now_and_never_tears_down_there(self):
        session = self.session(ATTEMPT_A)
        notify_thread = self.notify(session, ATTEMPT_A, FAILED)
        self.assertEqual(self.gate(), 0.0)                       # physical gate closed immediately
        self.assertEqual(session.null_threads, [])              # nothing native on the pc thread
        self.assertIs(self.engine.remote_dj_session, session)    # logical stop runs on the GLib thread
        self.run_idle()
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertEqual(session.connection_attempt.status, "failed")
        self.engine._remote_dj_server.retire_attempt_threadsafe.assert_called_once_with(ATTEMPT_A)
        self.slot.selector.set_property.assert_called_with("active-pad", self.slot.silence_pad)
        self.assertEqual(self.hold_threads, [threading.main_thread()])
        self.assertTrue(all(el.get_parent() is None for el in session.elements))   # detached first
        self.finish_cleanup()
        self.assertTrue(session.null_threads)
        self.assertNotIn(notify_thread, session.null_threads)
        self.assertNotIn(threading.main_thread(), session.null_threads)            # bounded worker
        self.assertIsNone(self.slot.session)

    # -- 2. successful cleanup -> slot reusable --------------------------

    def test_successful_cleanup_releases_the_slot_for_a_new_session(self):
        session = self.session(ATTEMPT_A)
        self.notify(session, ATTEMPT_A, FAILED)
        self.run_idle()
        self.assertIs(self.slot.session, session)               # held until native cleanup completes
        self.assertIsNone(self.engine._dj_slot_available())
        self.finish_cleanup()
        self.assertIsNone(self.slot.session)
        self.assertIsNone(self.slot.teardown)
        for element in session.elements:
            self.assertEqual(element.get_state(0).state, Gst.State.NULL)
        self.assertIs(self.engine._dj_slot_available(), self.slot)
        self.engine._remote_dj_build_session = lambda s: None
        with patch.object(eng.RemoteDJAudioInput, "load", return_value=MagicMock(gain_db=0.0)):
            self.engine._remote_dj_session_start(ATTEMPT_B, int(time.time() * 1000))
        self.assertEqual(self.engine.remote_dj_session.connection_attempt.attempt_id, ATTEMPT_B)
        self.assertIs(self.slot.session, self.engine.remote_dj_session)

    # -- 3. blocked native cleanup ---------------------------------------

    def test_blocked_native_cleanup_is_contained_quarantined_and_visible(self):
        session = self.session(ATTEMPT_A, stuck=True)
        self.notify(session, ATTEMPT_A, FAILED)
        started = time.monotonic()
        self.run_idle()                                          # GLib thread returns at once
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.gate(), 0.0)
        self.finish_cleanup()                                    # resolves via the timeout
        self.assertEqual(self.slot.teardown["state"], "quarantined")
        self.assertIs(self.slot.session, session)                # never advertised as reusable
        self.assertIsNone(self.engine._dj_slot_available())
        event = SystemEvent.objects.get(dedupe_key=f"engine|remote_dj|teardown_blocked|attempt={ATTEMPT_A}")
        self.assertEqual(event.level, "critical")
        self.assertTrue(event.detail["restart_required_to_reuse_remote_dj"])
        self.assertTrue(all(el.get_parent() is None for el in session.elements))
        with patch.object(eng, "emit_event") as emit:
            self.engine._remote_dj_session_start(ATTEMPT_B, int(time.time() * 1000))
        self.assertIn("engine restart required", emit.call_args.kwargs["detail"]["reason"])
        snap = self.engine._remote_dj_teardown_coordinator().snapshot()
        self.assertEqual((snap["worker_starts"], snap["poisoned"]), (1, True))   # bounded: one worker
        self.assertEqual(event.detail["teardown_event"], "timed_out")
        self.assertTrue(event.title.startswith("Remote DJ cleanup blocked;"))

    # -- 3b. failed native cleanup (r0132 B1) -----------------------------

    def _assert_failed_cleanup_quarantined(self, session, error_fragment):
        """FAILED retired and muted the session at once; the native cleanup
        then failed. The slot must stay quarantined -- never released --
        with one critical event naming the attempt and slot, and no retry."""
        self.assertIsNone(self.engine.remote_dj_session)            # logical session retired
        self.assertEqual(session.connection_attempt.status, "failed")
        self.assertEqual(self.gate(), 0.0)                          # mic gate closed
        self.slot.selector.set_property.assert_called_with("active-pad", self.slot.silence_pad)
        self.assertTrue(all(el.get_parent() is None for el in session.elements))
        self.assertEqual(self.timers, [])                           # poll resolved; nothing rescheduled
        self.assertEqual(self.slot.teardown["state"], "quarantined")
        self.assertIs(self.slot.session, session)                   # never released
        self.assertIsNone(self.engine._dj_slot_available())
        event = SystemEvent.objects.get(dedupe_key=f"engine|remote_dj|teardown_blocked|attempt={ATTEMPT_A}")
        self.assertEqual(event.level, "critical")
        self.assertTrue(event.title.startswith("Remote DJ cleanup failed;"))
        self.assertIn("engine restart", event.title)
        self.assertEqual(event.detail["teardown_event"], "failed")   # not "completed"
        self.assertEqual((event.detail["attempt_id"], event.detail["slot"]), (ATTEMPT_A, 0))
        self.assertIn(error_fragment, event.detail["error"])
        self.assertTrue(event.detail["restart_required_to_reuse_remote_dj"])
        snap = self.engine._remote_dj_teardown_coordinator().snapshot()
        self.assertEqual((snap["worker_starts"], snap["completed"]), (1, 1))  # one run, one worker
        self.assertEqual(len(session.null_threads), 1)              # stopped at the failure; no retry
        self.finish_cleanup()                                       # any further poll is a no-op
        self.assertIs(self.slot.session, session)
        with patch.object(eng, "emit_event") as emit:
            self.engine._remote_dj_session_start(ATTEMPT_B, int(time.time() * 1000))
        detail = emit.call_args.kwargs["detail"]
        self.assertIn("engine restart required", detail["reason"])
        self.assertIsNone(self.engine.remote_dj_session)            # B refused, A's slot untouched
        self.assertIs(self.slot.session, session)
        self.assertEqual(self.slot.teardown["state"], "quarantined")

    def test_native_cleanup_exception_quarantines_and_never_releases(self):
        session = self.session(ATTEMPT_A, null_fault="raise")
        self.notify(session, ATTEMPT_A, FAILED)
        self.run_idle()
        self.assertEqual(self.slot.teardown["state"], "cleaning")
        self.finish_cleanup()
        self._assert_failed_cleanup_quarantined(session, "simulated native teardown error")

    def test_native_null_failure_return_is_a_failed_cleanup_not_a_success(self):
        session = self.session(ATTEMPT_A, null_fault="failure")
        self.notify(session, ATTEMPT_A, FAILED)
        self.run_idle()
        self.finish_cleanup()
        self._assert_failed_cleanup_quarantined(session, "set_state(NULL) returned FAILURE")
        self.assertNotEqual(session.elements[0].get_state(0).state, Gst.State.NULL)

    # -- 4. stale callback -----------------------------------------------

    def test_a_late_callback_from_the_failed_session_cannot_touch_the_next_one(self):
        session_a = self.session(ATTEMPT_A)
        self.notify(session_a, ATTEMPT_A, FAILED)
        self.run_idle()
        self.finish_cleanup()
        session_b = self.session(ATTEMPT_B)                     # gate open, B live
        for state in (FAILED, CLOSED):
            self.notify(session_a, ATTEMPT_A, state)
        self.engine._remote_dj_fail_bound_session(session_a, ATTEMPT_A, eng.FAILURE_ICE_STATE, "late")
        self.run_idle()
        self.assertIs(self.engine.remote_dj_session, session_b)
        self.assertIs(self.slot.session, session_b)
        self.assertEqual(self.gate(), 1.0)
        self.assertEqual(session_b.connection_attempt.status, "connected")
        self.assertEqual(self.timers, [])

    # -- 5. repeated terminal notifications ------------------------------

    def test_failed_closed_and_disconnect_together_tear_down_once(self):
        session = self.session(ATTEMPT_A)
        self.notify(session, ATTEMPT_A, FAILED)
        self.notify(session, ATTEMPT_A, CLOSED)
        self.engine._remote_dj_session_stop(ATTEMPT_A)          # signaling disconnect on the GLib thread
        self.run_idle()
        self.finish_cleanup()
        self.engine._remote_dj_server.retire_attempt_threadsafe.assert_called_once_with(ATTEMPT_A)
        snap = self.engine._remote_dj_teardown_coordinator().snapshot()
        self.assertEqual((snap["completed"], snap["worker_starts"], snap["rejected"]), (1, 1, 0))
        self.assertEqual(len(session.null_threads), 2)          # each element NULLed exactly once
        self.assertIsNone(self.slot.session)

    # -- 6. shutdown -----------------------------------------------------

    def _stop_engine_bounded(self):
        engine = self.engine
        engine._lock = threading.Lock()
        engine.decks = {}
        engine.loop = MagicMock()
        engine.loop.is_running.return_value = False
        engine._write_state = MagicMock()
        engine._start_next_clear_restore = MagicMock()
        # stop() runs on the GLib/main thread in production. If it ever waited
        # on the simulated wedge, this timer frees it after 5 s, so the test
        # fails on elapsed time instead of hanging.
        unwedge = threading.Timer(5, self.release.set)
        unwedge.start()
        self.addCleanup(unwedge.cancel)
        started = time.monotonic()
        engine.stop()
        return time.monotonic() - started

    def test_shutdown_after_a_blocked_cleanup_does_not_stall(self):
        session = self.session(ATTEMPT_A, stuck=True)
        self.notify(session, ATTEMPT_A, FAILED)
        self.run_idle()
        self.finish_cleanup()
        self.assertEqual(self.slot.teardown["state"], "quarantined")
        self.assertLess(self._stop_engine_bounded(), 2.0)
        self.assertEqual(self.engine.main_pipeline.get_state(0).state, Gst.State.NULL)

    def test_shutdown_with_a_live_session_whose_cleanup_would_block_does_not_stall(self):
        self.session(ATTEMPT_A, stuck=True)
        with patch.object(eng, "emit_event"):
            self.assertLess(self._stop_engine_bounded(), 2.0)
        self.assertIsNone(self.engine.remote_dj_session)
        self.engine._remote_dj_server.retire_attempt_threadsafe.assert_called_once_with(ATTEMPT_A)

    # -- 7. automation / mic ownership -----------------------------------

    def test_ownership_reevaluation_and_mute_run_once_on_the_glib_thread(self):
        session = self.session(ATTEMPT_A)
        self.notify(session, ATTEMPT_A, FAILED)
        self.assertEqual(self.hold_threads, [])                 # never decided on the pc thread
        self.run_idle()
        self.assertEqual(self.hold_threads, [threading.main_thread()])
        self.assertEqual(self.gate(), 0.0)
        self.assertFalse(self.engine.manual_mode)               # Remote DJ teardown alone never asserts Manual
        self.finish_cleanup()

    def test_a_stale_off_thread_stop_never_mutes_a_newer_session(self):
        session_a = self.session(ATTEMPT_A, live=False)
        self.engine._remote_dj_session_stop(ATTEMPT_A)
        self.finish_cleanup()
        self.session(ATTEMPT_B)
        def late_stop():
            try:
                self.engine._remote_dj_session_stop(ATTEMPT_A)
            finally:
                connections.close_all()
        thread = threading.Thread(target=late_stop)
        thread.start()
        thread.join(5)
        self.assertEqual(self.gate(), 1.0)                      # B's mic untouched by A's late stop
        self.run_idle()
        self.assertEqual(self.engine.remote_dj_session.connection_attempt.attempt_id, ATTEMPT_B)
        del session_a

    # -- 8. normal disconnect --------------------------------------------

    def test_ordinary_disconnect_still_stops_and_releases(self):
        session = self.session(ATTEMPT_A)
        self.engine._remote_dj_session_stop(ATTEMPT_A)          # signaling disconnect, GLib thread
        self.assertEqual(self.idle, [])                          # no deferral needed on the GLib thread
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertEqual(self.gate(), 0.0)
        self.assertEqual(session.connection_attempt.status, "ended")
        self.assertIn("session_stopped", session.connection_attempt.milestones["server"])
        self.finish_cleanup()
        self.assertIsNone(self.slot.session)
        self.assertNotIn(threading.main_thread(), session.null_threads)

    def test_a_session_without_native_elements_releases_synchronously(self):
        session = self.session(ATTEMPT_A)
        for element in session.elements:
            self.engine.main_pipeline.remove(element)
            Gst.Element.set_state(element, Gst.State.NULL)
        session.elements.clear()
        self.engine._remote_dj_session_stop(ATTEMPT_A)
        self.assertIsNone(self.slot.session)
        self.assertEqual(self.timers, [])

    # -- real webrtcbin: the actual pc thread ----------------------------

    def test_real_webrtcbin_pc_thread_failure_defers_and_the_worker_nulls_it(self):
        """A real webrtcbin replies to a create-offer GstPromise on its own pc
        thread -- the thread that also delivers notify::connection-state, and
        the one _remote_dj_on_offer_created runs on. A teardown request made
        there must return at once (r0131 would NULL the element right there and
        never return -- deliberately NOT run against r0131), and the bounded
        worker then NULLs the real webrtcbin from another thread."""
        if Gst.ElementFactory.find("webrtcbin") is None:
            self.skipTest("webrtcbin plugin unavailable")
        session = self.session(ATTEMPT_A)
        for element in session.elements:
            self.engine.main_pipeline.remove(element)
            Gst.Element.set_state(element, Gst.State.NULL)
        webrtc = Gst.ElementFactory.make("webrtcbin", None)
        self.engine.main_pipeline.add(webrtc)
        webrtc.sync_state_with_parent()
        session.webrtc = webrtc
        session.elements[:] = [webrtc]
        callback_threads, returned = [], threading.Event()

        def on_offer_reply(_promise, *_args):
            callback_threads.append(threading.current_thread())
            self.engine._remote_dj_fail_bound_session(
                session, ATTEMPT_A, eng.FAILURE_ICE_STATE, "test failure on the pc thread")
            returned.set()
        webrtc.emit(
            "add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY,
            Gst.Caps.from_string("application/x-rtp,media=audio,encoding-name=OPUS,payload=96,clock-rate=48000"),
        )
        webrtc.emit("create-offer", None, Gst.Promise.new_with_change_func(on_offer_reply, None))
        self.assertTrue(returned.wait(5), "the pc-thread callback must return")
        self.assertIsNot(callback_threads[0], threading.main_thread())
        self.assertEqual(self.gate(), 0.0)
        self.assertIs(self.engine.remote_dj_session, session)         # deferred, not torn down there
        self.run_idle()
        self.assertIsNone(webrtc.get_parent())
        self.finish_cleanup()
        self.assertEqual(webrtc.get_state(0).state, Gst.State.NULL)
        self.assertIsNone(self.slot.session)
