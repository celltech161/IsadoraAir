"""Roadmap 2.25 B7 supplemental -- Remote DJ over a REAL WebRTC media path,
with VT transitions, measured in rendered program PCM.

The engine's own Remote DJ session (webrtcbin, ICE, DTLS-SRTP, Opus decode,
persistent DJ slot and gate) connected over loopback to an in-process
synthetic browser (library/tests/_vt_b7s_remote_lab.py) that sends a 3200 Hz
microphone; a 3000 Hz studio mic; VT speech at 1000 Hz (intro) / 900 Hz
(outro). Database work for the VT path is replaced by the integration lab's
seams; Remote DJ configuration is real (PostgreSQL rows).

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TransactionTestCase

import library.services.engine as eng_module
from library.tests._vt_b7s_remote_lab import (
    DJ_TONE, STUDIO_TONE, BrowserPeer, SignalingRelay, build_remote_topology, loopback_only, wait_connected,
)
from library.tests._vt_integration_lab import (
    HEAD, MUSIC, OUTRO_SPEECH, SPEECH, IntegrationLab, fake_voicetrack, pump, report, wait_until,
)
from library.tests.test_vt_b4_intro_integration import music

FADE_LIMIT = 0.07


class _RemoteBase:
    def remote_lab(self, *, intro_vt=3.0, outro_vt=None, in_intro=1.75, auto_manual=True, out_outro=None,
                   mic_duck=False):
        from library.models import RemoteDJConfig
        RemoteDJConfig.objects.update_or_create(pk=1, defaults={"enabled": True, "stun_server": "",
                                                                "reconnect_grace_seconds": 4})

        lab = IntegrationLab(self, ramp_ms=100)
        engine = lab.engine
        runtime = lab.root / "run-isadoraair"
        for name in dir(eng_module):
            value = getattr(eng_module, name)
            if isinstance(value, Path) and str(value).startswith("/run/isadoraair"):
                # Station runtime paths stay untouched: diagnostics land in the lab.
                moved = patch.object(eng_module, name, new=runtime / value.relative_to("/run/isadoraair"))
                moved.start()
                self.addCleanup(moved.stop)
        studio_gate = build_remote_topology(engine, lab.rate)
        self.studio_gate = studio_gate
        relay = SignalingRelay(self)
        engine._remote_dj_server = relay
        self.relay, self.lab, self.peer = relay, lab, None
        self.engine = engine           # the browser peer reaches the engine's signaling entry points
        cfg = SimpleNamespace(ptt_auto_manual_enabled=auto_manual, enabled=mic_duck, duck_level_db=-10.0,
                              ramp_ms=50, attack_ms=50, release_ms=50)
        engine.__dict__.setdefault("_duck_ramp_source_id", None)
        mocked = patch.object(eng_module.DuckingConfig, "load", new=lambda: cfg)
        mocked.start()
        self.addCleanup(mocked.stop)
        build = engine._remote_dj_build_session

        def build_with_loopback(session):
            out = build(session)
            loopback_only(session.ice_agent)
            return out
        engine._remote_dj_build_session = build_with_loopback
        out = lab.item(1, [(9.0, 400)], next_start=8.0, outro_starts=out_outro, name="rdj-out")
        inc = lab.item(2, music(0.25, in_intro, after=1.0), cue_in=0.25, intro_until=in_intro, name="rdj-in")
        lab.queue(out, inc)
        if intro_vt:
            lab.attach(inc, "intro", fake_voicetrack(lab.vt(f"rdj-intro-{intro_vt}", intro_vt, speech=SPEECH),
                                                     intro_vt))
        if outro_vt:
            lab.attach(out, "outro", fake_voicetrack(lab.vt(f"rdj-outro-{outro_vt}", outro_vt,
                                                            speech=OUTRO_SPEECH), outro_vt))
        lab.poll()
        lab.play()
        deck = lab.start("A")
        return lab, lab.media_zero(deck)

    # -- the stand-in websocket ---------------------------------------------------
    def peer_receive(self, attempt_id, message):
        if self.peer is None or self.peer.attempt_id != attempt_id:
            return
        if message.get("type") == "offer":
            try:
                self.peer.on_offer(message["sdp"])
            except Exception:
                import traceback
                print("RDJ-PEER-ERROR " + traceback.format_exc().replace("\n", " | "), flush=True)
                raise
        elif message.get("type") == "ice" and message.get("candidate"):
            self.peer.on_ice(message["sdpMLineIndex"], message["candidate"])

    def connect(self, timeout=15.0):
        engine = self.lab.engine
        self.assertTrue(wait_until(lambda: engine.remote_dj_session is None and engine._dj_slot_available() is not None,
                                   20.0), "the DJ slot is free (previous session's bounded teardown done)")
        attempt = uuid.uuid4().hex
        self.peer = BrowserPeer(self, attempt)
        self.addCleanup(self.peer.drop)
        started = self.lab.now()
        self.lab.engine._remote_dj_session_start(attempt_id=attempt)      # a GLib callback: returns False
        self.assertIsNotNone(self.lab.engine.remote_dj_session, "the engine's session was built")
        connected = wait_connected(self.lab, timeout)
        report("B7S-rdj-connect", connected=connected, seconds=self.lab.now() - started,
               relayed=",".join(sorted({t for _a, t in self.relay.sent})), answer=self.peer.answer_result,
               browser_ice=self.peer.webrtc.get_property("ice-connection-state").value_nick,
               browser_conn=self.peer.webrtc.get_property("connection-state").value_nick)
        print("RDJ-CANDIDATES engine=" + json.dumps(self.relay.candidates[:6]) + " browser="
              + json.dumps(self.peer.candidates[:6]), flush=True)
        self.assertTrue(connected, "ICE/DTLS connected over loopback")
        self.assertTrue(wait_until(lambda: getattr(self.lab.engine.remote_dj_session, "media_arrived", False)
                                   or self.lab.engine._remote_dj_connection_state() is not None, 5.0))
        return attempt


class RemoteDjMediaPathTests(_RemoteBase, TransactionTestCase):
    def test_connect_mic_off_then_on_reaches_the_program_and_vt_speech_yields(self):
        lab, zero = self.remote_lab(intro_vt=None)
        engine = lab.engine
        self.connect()
        pump(1.5)
        tr = lab.trace()
        off_level = tr.level(DJ_TONE, lab.now() - 1.0, lab.now())
        on_at = lab.now()
        engine._remote_dj_set_gate(True)
        pump(1.5)
        tr = lab.trace()
        onset = tr.onset(DJ_TONE, after=on_at - 0.1)
        from library.tests._vt_b7s_remote_lab import describe_transceivers
        print("RDJ-TRANS browser " + describe_transceivers(self.peer.webrtc, getattr(self.peer, "sinkpad", None))
              + " || engine " + describe_transceivers(engine.remote_dj_session.webrtc), flush=True)
        print(f"RDJ-FLOW browser_sent={getattr(self.peer, 'sent_buffers', None)} "
              f"browser_return={self.peer.return_buffers} src_state={self.peer.src.get_state(0)[1].value_nick}",
              flush=True)
        report("B7S-rdj-media", mic_off_level=off_level, dj_onset_after_gate=None if onset is None else onset - on_at,
               dj_level=tr.level(DJ_TONE, on_at + 0.5, lab.now()))
        self.assertLess(off_level, 0.01, "Microphone Off on connection: nothing reaches the program")
        self.assertIsNotNone(onset, "the remote microphone reaches the program mixer")
        self.assertLess(onset - on_at, 0.5)
        engine._remote_dj_set_gate(False)


def _overlap(tr, a_freq, b_freq, start, end, threshold=0.03):
    """Seconds (10 ms bins) where both tones are present at once."""
    a = {round(t, 2) for t, v in tr.coarse(a_freq) if start <= t < end and v >= threshold}
    b = {round(t, 2) for t, v in tr.coarse(b_freq) if start <= t < end and v >= threshold}
    return len(a & b) * 0.01


class RemoteDjVtInteractionTests(_RemoteBase, TransactionTestCase):
    """Real WebRTC remote microphone against real VT transitions."""

    def test_remote_mic_during_outro_speech_fades_it_and_never_competes(self):
        lab, zero = self.remote_lab(intro_vt=None, outro_vt=1.6, out_outro=5.0, auto_manual=False)
        engine = lab.engine
        self.connect()
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 5.5, 12.0))
        on_at = lab.now()
        engine._remote_dj_set_gate(True)
        pump(1.5)
        tr = lab.trace()
        end = tr.end(OUTRO_SPEECH, after=on_at - 0.12)
        overlap = _overlap(tr, OUTRO_SPEECH, DJ_TONE, on_at + FADE_LIMIT, lab.now())
        report("B7S-rdj-outro", speech_end_after_gate=None if end is None else end - on_at, overlap_s=overlap,
               dj_present=tr.present(DJ_TONE, on_at, lab.now()), vt_duck=lab.curve_value())
        self.assertLess(end - on_at, FADE_LIMIT, "recorded speech yields to the live remote mic")
        self.assertEqual(overlap, 0.0, "no competing speech")
        self.assertGreater(tr.present(DJ_TONE, on_at, lab.now()), 0.8)
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3, msg="VT duck released; the mic duck is its own")

    def test_remote_mic_during_held_music_holds_it_and_release_starts_the_same_song(self):
        lab, zero = self.remote_lab(intro_vt=3.0, in_intro=1.75)        # U = 1.65 s: music held
        engine = lab.engine
        self.connect()
        a_seg = zero + 8.0
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 12.0))
        on_at = lab.now()
        engine._remote_dj_set_gate(True)
        pump(2.5)
        self.assertTrue(engine.manual_mode, "the mic hold")
        held = engine.__dict__.get("_vtt_held_occurrence")
        self.assertEqual(None if held is None else held["log_item"].id, 2)
        tr = lab.trace()
        self.assertIsNone(tr.onset(MUSIC, after=a_seg - 0.5), "no automatic admission while the remote mic is live")
        off_at = lab.now()
        engine._remote_dj_set_gate(False)
        pump(1.5)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=off_at)
        report("B7S-rdj-held", speech_end_after_gate=tr.end(SPEECH, after=on_at - 0.12) - on_at,
               music_after_release=None if music_on is None else music_on - off_at)
        self.assertLess(tr.end(SPEECH, after=on_at - 0.12) - on_at, FADE_LIMIT)
        self.assertIsNotNone(music_on)
        self.assertIsNone(tr.onset(HEAD, after=off_at), "deferred handoff: no intro VT (D5)")
        self.assertEqual(set(lab.claims), {1, 2})

    def test_involuntary_loss_recovers_boundedly_and_reconnect_with_mic_off_keeps_vts(self):
        lab, zero = self.remote_lab(intro_vt=1.2, in_intro=4.25)
        engine = lab.engine
        self.connect()
        engine._remote_dj_set_gate(True)
        pump(0.5)
        lost_at = lab.now()
        self.peer.drop()                                   # the browser vanishes
        self.assertTrue(wait_until(lambda: engine.remote_dj_session is None, 30.0), "bounded recovery")
        recovered = lab.now() - lost_at
        tr = lab.trace()
        dj_after = tr.present(DJ_TONE, lost_at + 1.0, lab.now())
        self.assertFalse(engine.manual_mode, "the mic hold is released when the session ends")
        # Reconnect with the microphone Off: the next transition's VT is eligible.
        self.peer = None
        self.connect()
        self.assertFalse(engine._vtt_live_context().remote_dj_live)
        a_seg = zero + 8.0
        if lab.now() < a_seg - 1.0:
            self.assertTrue(wait_until(lambda: lab.now() > a_seg + 1.4, 20.0))
            head = lab.trace().onset(HEAD, after=a_seg - 1.0)
            vt_error = None if head is None else head - a_seg
        else:
            vt_error = "transition_passed"
        report("B7S-rdj-loss", recovered_s=recovered, dj_after_loss_s=dj_after, vt_onset_after_reconnect=vt_error,
               stale_ignored=sum(1 for _a, _t in self.relay.sent))
        self.assertLess(recovered, 20.0)
        self.assertLess(dj_after, 0.05, "the gate is forced closed during recovery")
        if isinstance(vt_error, float):
            self.assertLess(abs(vt_error), 0.015, "VTs eligible again after a reconnect with the mic Off")

    def test_rapid_gate_changes_during_outro_speech_never_let_vt_speech_compete(self):
        lab, zero = self.remote_lab(intro_vt=None, outro_vt=1.6, out_outro=5.0, auto_manual=False)
        engine = lab.engine
        self.connect()
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 5.3, 12.0))
        start = lab.now()
        for n in range(9):
            engine._remote_dj_set_gate(n % 2 == 0)
            pump(0.12)
        pump(1.0)
        tr = lab.trace()
        speech_end = tr.end(OUTRO_SPEECH, after=start - 0.12)
        overlap = _overlap(tr, OUTRO_SPEECH, DJ_TONE, start + FADE_LIMIT, lab.now())
        self.assertTrue(wait_until(lambda: 2 in lab.claims, 10.0))
        pump(1.0)
        report("B7S-rdj-rapid", speech_end_after_first_gate=speech_end - start, overlap_s=overlap,
               speech_after_s=lab.trace().present(OUTRO_SPEECH, start + FADE_LIMIT, lab.now()), claims=lab.claims)
        self.assertLess(speech_end - start, FADE_LIMIT, "the first gate cancels the outro; it never resumes")
        self.assertEqual(overlap, 0.0)
        self.assertLess(lab.trace().present(OUTRO_SPEECH, start + FADE_LIMIT, lab.now()), 0.011)
        self.assertEqual(lab.claims.count(2), 1, "the next song starts exactly once")

    def test_concurrent_studio_mic_and_remote_dj_hold_until_both_release(self):
        lab, zero = self.remote_lab(intro_vt=3.0, in_intro=1.75, mic_duck=True)   # U = 1.65 s
        engine = lab.engine
        self.connect()
        a_seg = zero + 8.0
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.3, 12.0))
        engine.mic_live = True                                  # studio mic on: mic hold
        engine._b7s_studio_gate.set_property("volume", 1.0)
        engine._apply_mic_mode_hold()
        engine._remote_dj_set_gate(True)                        # remote DJ joins
        pump(1.2)
        tr = lab.trace()
        both = (tr.present(DJ_TONE, lab.now() - 1.0, lab.now()) > 0.8
                and tr.present(STUDIO_TONE, lab.now() - 1.0, lab.now()) > 0.8)
        mic_duck = engine.duck_gain.get_property("volume")
        engine.mic_live = False                                 # studio mic released first
        engine._b7s_studio_gate.set_property("volume", 0.0)
        engine._apply_mic_mode_hold()
        pump(0.8)
        still_held = engine.manual_mode and engine.__dict__.get("_vtt_held_occurrence") is not None
        no_music = lab.trace().onset(MUSIC, after=a_seg - 0.5) is None
        off_at = lab.now()
        engine._remote_dj_set_gate(False)
        pump(1.5)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=off_at)
        report("B7S-rdj-concurrent", both_mics=both, mic_duck=mic_duck, vt_duck=lab.curve_value(),
               held_after_studio_release=still_held, music_after_last_release=None if music_on is None else
               music_on - off_at, speech_during_mics=tr.present(SPEECH, a_seg + 0.3 + FADE_LIMIT, off_at))
        self.assertTrue(both, "studio and remote microphones both reach the program")
        self.assertLess(mic_duck, 0.5, "the mic duck engages on its own control")
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3, msg="the VT duck is released (independent)")
        self.assertTrue(still_held and no_music, "releasing one microphone never admits while the other is live")
        self.assertIsNotNone(music_on)
        self.assertIsNone(tr.onset(HEAD, after=off_at), "deferred handoff: no intro VT")
        self.assertEqual(set(lab.claims), {1, 2})
        self.assertLess(tr.present(SPEECH, a_seg + 0.3 + FADE_LIMIT, off_at), 0.011, "no VT speech over live mics")

    def test_reconnect_during_vt_recovery_airs_the_claimed_song_once(self):
        from library.tests.test_vt_b7_qualification import _after_fallback, _stall_first_readiness
        lab, zero = self.remote_lab(intro_vt=3.0, in_intro=1.75)
        engine = lab.engine
        self.connect()
        _stall_first_readiness(engine)
        lost = {}

        def lose_and_reconnect():
            lost["t"] = lab.now()
            self.peer.drop()
        _after_fallback(lab, lose_and_reconnect)
        a_seg = zero + 8.0
        self.assertTrue(wait_until(lambda: "t" in lost, 14.0))
        self.assertTrue(wait_until(lambda: engine.remote_dj_session is None, 30.0))
        self.peer = None
        self.connect()
        pump(2.0)
        report("B7S-rdj-recovery", claims=lab.claims, air_starts=len([a for a in lab.air_starts if a["log_item_id"] == 2]))
        self.assertEqual(len([a for a in lab.air_starts if a["log_item_id"] == 2]), 1, "aired exactly once")
        self.assertIsNotNone(lab.trace().onset(MUSIC, after=a_seg - 1.0))
