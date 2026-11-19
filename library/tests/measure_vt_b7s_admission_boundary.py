"""Roadmap 2.25 B7 supplemental -- where does a scheduled admission become
irreversible? (MEASUREMENT; no ``test_`` prefix: run explicitly.)

A held, never-audible claimed occurrence is withdrawn (the B5 Manual hold)
at offsets before its scheduled admission, with the engine's proximity rule
bypassed, and the rendered program PCM is checked for ANY of its audio. A
buffer probe on the deck mixer's src pad records how far the non-live deck
mixer has already aggregated and pushed ahead of the pipeline clock at the
moment of the hold: once it has pushed output covering the admission, the
deck's first samples are downstream and cannot be recalled.

Incoming music starts at media 0 (Cue In 0), so any admitted sample is tone.
"""

from __future__ import annotations

import json

from django.test import SimpleTestCase

from library.tests._vt_b0_lab import GLib, Gst
from library.tests._vt_integration_lab import MUSIC, NS, pump, wait_until
from library.tests.test_vt_b5_operator_integration import _B5Base

OFFSETS_MS = (120, 80, 60, 45, 35, 25, 18, 12, 6, 0)


class MixerAhead:
    """Latest running end time the deck mixer has pushed downstream."""

    def __init__(self, engine):
        self.until_ns = 0
        pad = engine.mixer.get_static_pad("src")
        self.pad, self.probe = pad, pad.add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)

    def _on_buffer(self, pad, info):
        buf = info.get_buffer()
        if buf is not None and buf.pts != Gst.CLOCK_TIME_NONE:
            seg = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
            pts = buf.pts
            if seg is not None:
                running = seg.parse_segment().to_running_time(Gst.Format.TIME, pts)
                if running != Gst.CLOCK_TIME_NONE:
                    pts = running
            end = pts + (buf.duration if buf.duration != Gst.CLOCK_TIME_NONE else 0)
            if end > self.until_ns:
                self.until_ns = end
        return Gst.PadProbeReturn.OK


class AdmissionBoundary(_B5Base, SimpleTestCase):
    def one(self, offset_ms, *, busy=False, manual=True, engine_rule=False):
        lab, out, inc, deck, zero = self.build(in_cue=0.0, in_intro=1.5, intro_vt=3.0)
        engine = lab.engine
        ahead = MixerAhead(engine)
        if busy:
            def spin():
                import time as _t
                t0 = _t.monotonic()
                while _t.monotonic() - t0 < 0.015:
                    pass
                return True
            source = GLib.timeout_add(50, spin)
            self.addCleanup(lambda: GLib.MainContext.default().find_source_by_id(source) and GLib.source_remove(source))
        if not manual:
            self.ducking(lab, auto_manual=True)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.deck_of(2) is not None
                                   and lab.deck_of(2).admission_state == "scheduled", 6.0))
        admission = lab.deck_of(2).scheduled_admission_ns
        if not engine_rule:
            # Bypass the engine's rule: what matters here is what the pipeline does.
            engine._vtt_incoming_on_air = lambda d, now_ns=None: getattr(d, "admission_state", None) in (None, "audible")
        hit = {}

        def hold():
            hit["now_ns"] = lab.now_ns()
            hit["ahead_ns"] = ahead.until_ns - hit["now_ns"]
            hit["pushed_past_admission"] = ahead.until_ns > admission
            hit["state"] = getattr(lab.deck_of(2), "admission_state", None)
            hit["rule_on_air"] = lab.deck_of(2) is None or engine._vtt_incoming_on_air(lab.deck_of(2))
            if manual:
                engine._set_manual_mode(True)
            else:
                lab.mic(True)
            hit["held"] = engine.__dict__.get("_vtt_held_occurrence") is not None
        lab.lab.at(admission / NS - offset_ms / 1000.0, hold)
        self.assertTrue(wait_until(lambda: "held" in hit, 12.0))
        pump(0.8)
        tr = lab.trace()
        leaked = tr.present(MUSIC, admission / NS - 0.05, admission / NS + 0.6)
        row = {"offset_ms": offset_ms, "busy": busy, "authority": "manual" if manual else "mic",
               "engine_rule": engine_rule, "rule_on_air": hit["rule_on_air"],
               "lead_ms": round((admission - hit["now_ns"]) / 1e6, 2),
               "mixer_ahead_ms": round(hit["ahead_ns"] / 1e6, 2),
               "pushed_past_admission": hit["pushed_past_admission"], "state": hit["state"],
               "held": hit["held"], "leaked_s": round(leaked, 4),
               "air_starts": len(self.air_starts(lab, 2))}
        print("VT-BOUNDARY " + json.dumps(row, sort_keys=True), flush=True)
        if engine_rule:
            if row["held"]:
                self.assertEqual(row["leaked_s"], 0.0, "a held (recallable) song never renders a sample")
            else:
                self.assertGreater(row["leaked_s"], 0.55, "an irreversible song plays in full, never partially")
                self.assertEqual(row["air_starts"], 1)
        return row



def _make(offset, rep, busy, manual, engine_rule=False):
    def test(self):
        self.one(offset, busy=busy, manual=manual, engine_rule=engine_rule)
    return test


RULE_OFFSETS_MS = (60, 45, 35, 30, 25, 20, 15, 10, 5, 0)
for _offset in RULE_OFFSETS_MS:
    for _rep in range(3):
        for _name, _busy, _manual in (("rule_manual_idle", False, True), ("rule_manual_busy", True, True),
                                      ("rule_mic_idle", False, False)):
            setattr(AdmissionBoundary, f"test_{_name}_{_offset:03d}ms_r{_rep}",
                    _make(_offset, _rep, _busy, _manual, engine_rule=True))


for _offset in OFFSETS_MS:
    for _rep in range(3):
        for _name, _busy, _manual in (("manual_idle", False, True), ("manual_busy", True, True),
                                      ("mic_idle", False, False)):
            setattr(AdmissionBoundary, f"test_{_name}_{_offset:03d}ms_r{_rep}", _make(_offset, _rep, _busy, _manual))
