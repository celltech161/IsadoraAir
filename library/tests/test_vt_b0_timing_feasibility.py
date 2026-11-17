"""Roadmap 2.25 Phase B0 -- real-GStreamer timing feasibility (isolated).

No production engine change. Each experiment builds the r0144 program
topology (transport harness), plays synthetic tone media through the REAL
deck bins and a prototype scheduled VT branch (library/tests/_vt_b0_lab.py),
records the rendered program PCM at a clock-synced appsink and measures
every onset/end at sample resolution against the PLANNED running time.

Signals (multiples of 100 Hz: exactly orthogonal in the 10 ms analysis
window):
  OUT 400 outgoing music, MUSIC 600 incoming music before Intro Until,
  VOCAL 1400 incoming music from Intro Until (the vocal entry),
  SPEECH 1000 VT speech, HEAD 2200 the first 150 ms of every VT,
  OUTRO 800 outro VT speech, MIC 3000 live microphone.

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import os
import threading
import time

from django.test import SimpleTestCase

from library.tests._vt_b0_lab import NS, P, GLib, Gst, Lab, pump, report, stats, wait_until

OUT, MUSIC, VOCAL, SPEECH, HEAD, OUTRO, MIC = 400, 600, 1400, 1000, 2200, 800, 3000
M = 0.150          # approved vocal safety margin (D11)
HEAD_S = 0.150     # head marker length in every VT file
TOL = 0.015        # the approved D15 target (upper bound of +-10-15 ms)


def vt_segments(total, speech=SPEECH):
    return [(HEAD_S, HEAD), (total - HEAD_S, speech)]


def music_segments(cue_in, intro_until, after=0.6, length=None):
    """Silence to Cue In, MUSIC to Intro Until, then VOCAL. ``intro_until``
    None: MUSIC for ``length`` seconds then VOCAL."""
    segs = []
    if cue_in > 0:
        segs.append((cue_in, 0))
    if intro_until is None:
        segs.append((length, MUSIC))
    else:
        segs.append((intro_until - cue_in, MUSIC))
    segs.append((after, VOCAL))
    return segs


def underlap(v, c, i):
    """Approved model: W = max(0, I - C); U = max(0, V - W + M) when a
    usable window exists; otherwise the whole VT before the music."""
    if i is None or i <= c:
        return v + M
    w = max(0.0, i - c)
    return max(0.0, v - w + M)


# ---------------------------------------------------------------------------
# Approach comparison: how precisely can a VT branch be made audible at a
# chosen running time T, with its head intact?
# ---------------------------------------------------------------------------
class ApproachComparisonTests(SimpleTestCase):
    TRIALS = 4

    def _run(self, rate, fmt):
        lab = Lab(self, rate=rate)
        lab.play()
        path = lab.media(f"vt-approach-{fmt}", vt_segments(0.55), fmt=fmt)
        results = {}
        trials = []
        base = lab.now() + 0.6
        approaches = ("link_now_timer", "scheduled_offset", "gated_timer", "gated_clock_thread")
        for rep in range(self.TRIALS):
            for name in approaches:
                target = base
                base += 0.9
                trials.append((name, target))
                if name == "scheduled_offset":
                    lab.at(target - 0.30, self._scheduled, lab, path, target)
                elif name == "link_now_timer":
                    lab.at(target, self._link_now, lab, path)
                elif name == "gated_timer":
                    lab.at(target - 0.30, self._gated, lab, path, target, False)
                else:
                    lab.at(target - 0.30, self._gated, lab, path, target, True)
        pump(base - lab.now() + 0.5)
        trace = lab.trace()
        for name, target in trials:
            on = trace.onset(HEAD, after=target - 0.2, before=target + 0.6)
            end = trace.end(HEAD, after=on + 0.05) if on is not None else None
            results.setdefault(name, []).append(
                (None if on is None else on - target, None if end is None else end - on))
        for branch in lab.branches:
            if branch.fx_pad is not None:
                branch._teardown()
        summary = {}
        for name in approaches:
            errs = [e for e, _h in results[name]]
            heads = [h for _e, h in results[name]]
            s = stats(errs)
            hs = stats(heads)
            summary[name] = (s, hs)
            report(f"APPROACH rate={rate} fmt={fmt} {name}", n=s["n"], mean_err=s.get("mean"),
                   min_err=s.get("min"), max_err=s.get("max"), head_min=hs.get("min"), head_max=hs.get("max"))
        return summary

    def _scheduled(self, lab, path, target):
        lab.branch(path, side=f"cmp{target}", target_ns=int(target * NS), duration=0.55, mode="scheduled").start()

    def _link_now(self, lab, path):
        lab.branch(path, side="cmp", target_ns=0, duration=0.55, mode="link_now").start()

    def _gated(self, lab, path, target, use_clock):
        branch = lab.branch(path, side=f"cmp{target}", target_ns=int(target * NS), duration=0.55, mode="gated")
        branch.start()
        if use_clock:
            # PyGObject cannot marshal GstClockID (a raw pointer) for
            # id_wait_async, so the clock-driven variant is a dedicated
            # thread that waits on the pipeline clock itself.
            def waiter():
                while lab.now() < target - 0.002:
                    time.sleep(0.001)
                while lab.now() < target:
                    pass
                branch.admit()
            threading.Thread(target=waiter, daemon=True).start()
        else:
            lab.at(target, branch.admit)

    def _assert_scheduled(self, summary):
        s, hs = summary["scheduled_offset"]
        self.assertEqual(s["n"], self.TRIALS)
        self.assertLessEqual(s["max_abs"], TOL)
        self.assertGreaterEqual(hs["min"], HEAD_S - TOL)

    def test_approaches_44100_wav(self):
        self._assert_scheduled(self._run(44100, "wav"))

    def test_approaches_48000_flac(self):
        self._assert_scheduled(self._run(48000, "flac"))


# ---------------------------------------------------------------------------
# Deck admission at a future running time through the REAL _create_deck.
# ---------------------------------------------------------------------------
class DeckScheduledAdmissionTests(SimpleTestCase):
    def test_deck_future_offset_onset_and_readiness(self):
        for rate, fmt in ((44100, "wav"), (44100, "flac"), (48000, "wav"), (48000, "flac")):
            with self.subTest(rate=rate, fmt=fmt):
                if getattr(self, "_previous_lab", None) is not None:
                    self._previous_lab.stop()
                lab = self._previous_lab = Lab(self, rate=rate)
                lab.play()
                cue = 0.25
                path = lab.media(f"deck-{fmt}", music_segments(cue, 1.2), fmt=fmt)
                errs = []
                for rep in range(3):
                    item = lab.item(10 + rep, path, duration=1.8, cue_in=cue)
                    hold = 0.4 + 0.6 * rep              # admission 0.4 / 1.0 / 1.6 s after creation
                    offset = lab.now_ns() + int(hold * NS)
                    readiness = {}
                    deck = lab.create_deck("A", item, offset_ns=offset, readiness=readiness)
                    self.assertIsNotNone(deck)
                    planned = offset / NS + P + cue
                    pump(hold + P + 2.0)
                    trace = lab.trace()
                    on = trace.onset(MUSIC, after=planned - 0.5, before=planned + 1.0)
                    errs.append(on - planned)
                    report(f"DECK rate={rate} fmt={fmt} hold={hold:.1f}", planned=planned, onset=on,
                           err=on - planned, ready_before_audible=planned - readiness["ready_ns"] / NS,
                           ready_after_create=(readiness["ready_ns"] - deck.lab["created_ns"]) / NS)
                    self.assertLess(readiness["ready_ns"] / NS, planned)
                s = stats(errs)
                report(f"DECK-SUMMARY rate={rate} fmt={fmt}", n=s["n"], mean=s["mean"], max_abs=s["max_abs"])
                self.assertLessEqual(s["max_abs"], TOL)


# ---------------------------------------------------------------------------
# Readiness: when can the engine PROVE an incoming deck is viable?
# ---------------------------------------------------------------------------
class DeckReadinessTests(SimpleTestCase):
    """The r0144 primed deck (silence prime via ``concat``) only lets the
    decoder run once its prime starts for any media that needs conversion,
    so readiness is provable only P + Cue In before the music. A deck
    admitted directly by a future pad offset (no prime) decodes at once and
    waits in the mixer pad. Both are measured for onset and readiness."""

    def test_primed_vs_direct_scheduled_readiness(self):
        hold, cue = 2.0, 0.25
        for rate, fmt in ((44100, "wav"), (44100, "flac"), (48000, "wav"), (48000, "flac")):
            with self.subTest(rate=rate, fmt=fmt):
                if getattr(self, "_previous_lab", None) is not None:
                    self._previous_lab.stop()
                lab = self._previous_lab = Lab(self, rate=rate)
                lab.play()
                path = lab.media(f"ready-{fmt}", music_segments(cue, 1.0), fmt=fmt)
                # Primed (real _create_deck).
                readiness = {}
                created = lab.now()
                planned_primed = created + hold
                lab.create_deck("A", lab.item(20, path, duration=1.6, cue_in=cue),
                                offset_ns=int((planned_primed - P - cue) * NS), readiness=readiness)
                # Direct scheduled deck on the deck mixer (no prime).
                planned_direct = planned_primed + 2.5
                direct = lab.branch(path, side="deck-direct", target_ns=int((planned_direct - cue) * NS),
                                    duration=1.6, mixer=lab.engine.mixer)
                direct.start()
                direct_created = lab.now()
                pump(planned_direct - lab.now() + 2.0)
                trace = lab.trace()
                on_p = trace.onset(MUSIC, after=planned_primed - 0.5, before=planned_primed + 1.0)
                on_d = trace.onset(MUSIC, after=planned_direct - 0.5, before=planned_direct + 1.0)
                lead_p = planned_primed - readiness["ready_ns"] / NS
                lead_d = planned_direct - direct.ready_ns / NS
                report(f"READINESS rate={rate} fmt={fmt}", primed_err=on_p - planned_primed,
                       primed_ready_lead=lead_p, primed_ready_after_create=readiness["ready_ns"] / NS - created,
                       direct_err=on_d - planned_direct, direct_ready_lead=lead_d,
                       direct_ready_after_create=direct.ready_ns / NS - direct_created)
                self.assertLessEqual(abs(on_p - planned_primed), TOL)
                self.assertLessEqual(abs(on_d - planned_direct), TOL)
                self.assertGreater(lead_d, hold - 0.2)        # proven ~at creation
                self.assertGreater(lead_p, P + cue - 0.05)     # at least the prime window


# ---------------------------------------------------------------------------
# Transition experiments (approved model, executed with scheduled offsets).
# ---------------------------------------------------------------------------
def make_plan(t0, *, out_ns, c, i, v=None, outro=None, gap=0.2):
    o_s = o_end = s_v = u = None
    a_seg = t0 + P + out_ns                       # outgoing's Next Start, audible
    if outro is not None:
        o_s = t0 + P + outro[0]
        o_end = o_s + outro[1]
    a0 = max(a_seg, o_end) if o_end is not None else a_seg     # D3 hold
    if v is not None:
        s_v = max(a0, o_end + gap) if o_end is not None else a0
        u = underlap(v, c, i)
        a_i = s_v + u                                           # D1 / D2 / D10
    else:
        a_i = a0
    return SimpleNamespace(t0=t0, a_seg=a_seg, o_s=o_s, o_end=o_end, s_v=s_v, u=u, v=v,
                           v_end=None if v is None else s_v + v, a_i=a_i,
                           deck_offset=a_i - P - c, c=c, i=i)


def duck_points(intervals, ramp, depth):
    """Speech-owned duck (D12): attack one ramp BEFORE each speech start,
    release from each speech end. Overlapping/adjacent intervals merge."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start - ramp <= merged[-1][1] + ramp:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    points = [(0.0, 1.0)]
    for start, end in merged:
        points += [(start - ramp, 1.0), (start, depth), (end, depth), (end + ramp, 1.0)]
    return points


from types import SimpleNamespace  # noqa: E402


class _BranchReady(dict):
    """deck_ready view over a gated incoming branch."""

    def __init__(self, branch):
        super().__init__()
        self.branch = branch

    def __contains__(self, key):
        return key == "ready_ns" and self.branch.ready_ns is not None

    def __getitem__(self, key):
        return self.branch.ready_ns

    def get(self, key, default=None):
        return self.branch.ready_ns if key in self else default


class TransitionMixin:
    DUCK = 0.5          # -6 dB, the VoiceTrackConfig default
    RAMP = 0.1
    PREP = 0.35         # prepare a VT branch this long before admission

    def lab(self, rate=44100, mic=False):
        previous = getattr(self, "_previous_lab", None)
        if previous is not None:
            previous.stop()        # one live program pipeline at a time
        lab = Lab(self, rate=rate, mic_freq=MIC if mic else None)
        self._previous_lab = lab
        lab.play()
        lab.attach_duck_curve()
        return lab

    def transition(self, lab, label, *, out_len, out_ns, c, i, v=None, outro=None, gap=0.2,
                   fmt="wav", tail=0.6, no_intro_len=1.0, actions=(), vt_path=None,
                   deck_stall=0.0, vt_stall=0.0, vt_duration=None, measure=True, incoming="primed"):
        t0 = lab.now() + 0.5
        p = make_plan(t0, out_ns=out_ns, c=c, i=i, v=v, outro=outro, gap=gap)
        media_i = i if (i is not None and i > c) else None     # unusable marker: no window
        p.media_i, p.no_intro_len, p.out_len = media_i, no_intro_len, out_len
        out_path = lab.media(f"out-{out_len}-{fmt}", [(out_len, OUT)], fmt=fmt)
        in_path = lab.media(f"in-{c}-{media_i}-{no_intro_len}-{fmt}",
                            music_segments(c, media_i, after=tail, length=no_intro_len), fmt=fmt)
        in_len = (media_i if media_i else c + no_intro_len) + tail
        lab.create_deck("A", lab.item(1, out_path, duration=out_len, next_start=out_ns),
                        offset_ns=int(p.t0 * NS))
        speech = []
        p.outro_branch = p.intro_branch = None
        if outro is not None:
            outro_path = lab.media(f"outro-{outro[1]}-{fmt}", vt_segments(outro[1], OUTRO), fmt=fmt)
            lab.at(p.o_s - self.PREP, self._start_vt, lab, p, "outro", outro_path, p.o_s, outro[1], 0.0)
            speech.append((p.o_s, p.o_end))
        p.deck_ready = {}
        create_at = min(p.deck_offset, p.s_v if p.s_v is not None else p.deck_offset) - 0.45
        p.incoming_mode = incoming
        p.create_at = create_at
        lab.at(create_at, self._create_incoming, lab, p, in_path, in_len, c, i, deck_stall)
        if v is not None:
            path = vt_path or lab.media(f"intro-{v}-{fmt}", vt_segments(v), fmt=fmt)
            lab.at(p.s_v - self.PREP, self._start_vt, lab, p, "intro", path, p.s_v,
                   vt_duration or v, vt_stall)
            speech.append((p.s_v, p.v_end))
        p.duck = duck_points(speech, self.RAMP, self.DUCK)
        lab.duck_plan(p.duck)
        for when, fn in actions:
            lab.at(when(p), fn, lab, p)
        end = max(x for x in (p.a_i + (media_i - c if media_i else no_intro_len) + tail, p.v_end or 0,
                              p.o_end or 0, t0 + P + out_len) if x)
        pump(end - lab.now() + 0.6)
        p.trace = lab.trace()
        if measure:
            p.m = self.measure(p)
            report(label, **p.m)
        return p

    def _create_incoming(self, lab, p, path, length, c, i, stall):
        p.incoming_created = lab.now()
        if p.incoming_mode == "direct":
            # Recommended admission: no prime, pad offset A_I - C set before
            # start; decodes at once and waits in the mixer pad.
            branch = lab.branch(path, side="incoming", target_ns=int((p.a_i - c) * NS), duration=length,
                                mixer=lab.engine.mixer, stall_s=stall)
            branch.start()
            p.incoming = branch
            p.deck_ready = _BranchReady(branch)
            return
        if p.incoming_mode == "gated":
            # Recommended admission: no prime, held behind a gate (readiness
            # at once), committed COMMIT_LEAD before A_I - C.
            branch = lab.branch(path, side="incoming", target_ns=int((p.a_i - c) * NS), duration=length,
                                mixer=lab.engine.mixer, mode="gated_scheduled", stall_s=stall)
            branch.start()
            p.incoming = branch
            p.deck_ready = _BranchReady(branch)
            return
        p.incoming = lab.create_deck("B", lab.item(2, path, duration=length, cue_in=c, intro_until=i or 0.0),
                                     offset_ns=int(p.deck_offset * NS), readiness=p.deck_ready, stall_s=stall)

    def _start_vt(self, lab, p, side, path, target, duration, stall):
        if side == "intro":
            p.deck_ready_before_vt_start = "ready_ns" in p.deck_ready
            incoming = getattr(p, "incoming", None)
            if p.incoming_mode == "direct" and (incoming is None or incoming.ready_ns is None
                                                or incoming.state in ("failed", "cancelled")):
                # Viability gate (contract section 6): no speech for an unproven deck.
                p.intro_skipped = "incoming_not_ready"
                lab.duck_plan([(0.0, 1.0)])
                return
        branch = lab.branch(path, side=side, target_ns=int(target * NS), duration=duration, stall_s=stall)
        setattr(p, f"{side}_branch", branch)
        branch.start()

    def measure(self, p):
        t = p.trace
        m = {}
        out_on = t.onset(OUT, after=p.t0 - 0.2, before=p.t0 + 1.0)
        m["out_err"] = out_on - (p.t0 + P)
        # Outgoing continuity: fraction of its planned span actually rendered
        # (a starved deck mixer freezes every deck, see E12).
        # The last 0.25 s is excluded: r0144 deck retirement cuts deck tails
        # (F14b), measured separately as out_tail_err.
        span = p.out_len - 0.30
        m["out_continuity"] = t.present(OUT, p.t0 + P + 0.05, p.t0 + P + p.out_len - 0.25) / span
        out_end = t.end(OUT, after=p.t0 + P + p.out_len - 0.5)
        m["out_tail_err"] = None if out_end is None else out_end - (p.t0 + P + p.out_len)
        if p.o_s is not None:
            on = t.onset(HEAD, after=p.o_s - 0.3, before=p.o_s + 0.5)
            end = t.end(OUTRO, after=on + HEAD_S + 0.05)
            m.update(outro_err=on - p.o_s, outro_end_err=end - p.o_end, outro_len=end - on)
            p.outro_on, p.outro_end = on, end
        music_on = t.onset(MUSIC, after=p.a_i - 0.6, before=p.a_i + 1.0)
        m["music_err"] = None if music_on is None else music_on - p.a_i
        if p.s_v is not None:
            on = t.onset(HEAD, after=p.s_v - 0.3, before=p.s_v + 0.5)
            end = t.end(SPEECH, after=on + HEAD_S + 0.05) if on is not None else None
            p.vt_on, p.vt_end = on, end
            if on is not None:
                m.update(vt_err=on - p.s_v, vt_len=end - on, vt_len_err=(end - on) - p.v,
                         planned_u=p.u, measured_u=None if music_on is None else music_on - on,
                         head=t.end(HEAD, after=on + 0.02) - on)
            if p.o_end is not None and on is not None:
                m["speech_gap"] = on - p.outro_end
        vocal_planned = p.a_i + ((p.media_i - p.c) if p.media_i else p.no_intro_len)
        vocal_on = t.onset(VOCAL, after=vocal_planned - 0.5, before=vocal_planned + 1.0)
        m["vocal_err"] = None if vocal_on is None else vocal_on - vocal_planned
        if p.s_v is not None and getattr(p, "vt_end", None) is not None and vocal_on is not None:
            m["clearance"] = vocal_on - p.vt_end
        m["deck_ready_lead_vs_speech"] = (
            None if "ready_ns" not in p.deck_ready or p.s_v is None
            else p.s_v - p.deck_ready["ready_ns"] / NS)
        return m

    def assert_timing(self, p, *keys):
        self.assertGreaterEqual(p.m["out_continuity"], 0.97, "outgoing deck starved")
        for key in keys:
            value = p.m.get(key)
            self.assertIsNotNone(value, key)
            self.assertLessEqual(abs(value), TOL, f"{key}={value}")


class E01OrdinarySegueTests(SimpleTestCase):
    """1. Ordinary non-VT segue, compared against r0144 (unchanged path)."""
    RUNS = 5

    def test_e01_ordinary_segue_matches_r0144_baseline(self):
        from library.tests._transport_harness import TransportHarness, running_seconds
        from library.tests.test_vt_transition_2_25_phase_a import onset, patch_vt_lookup, start_poll_loop
        errors = []
        for run in range(self.RUNS):
            h = TransportHarness(self, appsink=True)
            patch_vt_lookup(self, {})
            h.queue({"id": 1, "seconds": 7.0, "freq": OUT, "next_start": 6.0},
                    {"id": 2, "seconds": 3.0, "freq": MUSIC, "lead_silence": 0.25, "cue_in": 0.25})
            start_poll_loop(self, h.engine)
            h.play()
            h.engine._start_next_track(slot="A")
            self.assertTrue(wait_until(lambda: h.claims == [1, 2], 12.0))
            pump(1.5)
            out_on = onset(h, OUT)
            in_on = onset(h, MUSIC, after=out_on + 1.0)
            errors.append(in_on - (out_on + 6.0))
            h.engine.main_pipeline.set_state(Gst.State.NULL)
        s = stats(errors)
        report("E01 ordinary_segue_r0144_path", n=s["n"], mean=s["mean"], min=s["min"], max=s["max"])
        # Phase A R0 baseline 0.78-0.79 s; B0 changes no production code, so
        # the ordinary path is r0144 by construction. Recorded, not tightened.
        self.assertGreaterEqual(s["min"], 0.0)
        self.assertLess(s["max"], 1.5)


class E02E06IntroTimingTests(TransitionMixin, SimpleTestCase):
    def test_e02_incident_short_intro_vt(self):
        """Incident markers: Cue In 0.124717, Intro Until 15.008, VT 6.456229 s -> U = 0."""
        lab = self.lab()
        p = self.transition(lab, "E02 incident_short_vt rate=44100 fmt=wav", out_len=2.0, out_ns=1.5,
                            c=0.124717, i=15.008, v=6.456229)
        self.assertEqual(p.u, 0.0)
        self.assert_timing(p, "out_err", "vt_err", "music_err", "vt_len_err", "vocal_err")
        self.assertLessEqual(abs(p.m["measured_u"]), TOL)
        self.assertGreaterEqual(p.m["clearance"], 14.883283 - 6.456229 - TOL)

    def test_e03_exact_fit_window_and_margin(self):
        for rate, fmt in ((44100, "wav"), (48000, "flac")):
            with self.subTest(rate=rate, fmt=fmt):
                lab = self.lab(rate)
                p = self.transition(lab, f"E03 exact_fit rate={rate} fmt={fmt}", out_len=1.6, out_ns=1.2,
                                    c=0.25, i=2.0, v=1.6, fmt=fmt)
                self.assertAlmostEqual(p.u, 0.0, places=9)
                self.assert_timing(p, "vt_err", "music_err", "vt_len_err", "vocal_err")
                self.assertLessEqual(abs(p.m["clearance"] - M), TOL)

    def test_e04_incident_long_vt_underlap(self):
        """Same markers, 18.0 s synthetic VT -> U = 3.266717 s."""
        lab = self.lab()
        p = self.transition(lab, "E04 incident_long_vt rate=44100 fmt=wav", out_len=2.0, out_ns=1.5,
                            c=0.124717, i=15.008, v=18.0)
        self.assertAlmostEqual(p.u, 18.0 - (15.008 - 0.124717) + M, places=6)
        self.assert_timing(p, "vt_err", "music_err", "vt_len_err", "vocal_err")
        self.assertLessEqual(abs(p.m["measured_u"] - p.u), TOL)
        self.assertLessEqual(abs(p.m["clearance"] - M), TOL)

    def test_e04b_scaled_underlap(self):
        for rate, fmt in ((44100, "flac"), (48000, "wav"), (48000, "flac")):
            with self.subTest(rate=rate, fmt=fmt):
                lab = self.lab(rate)
                p = self.transition(lab, f"E04b primed_underlap rate={rate} fmt={fmt}", out_len=1.6,
                                    out_ns=1.2, c=0.3, i=1.5, v=2.5, fmt=fmt)
                self.assertAlmostEqual(p.u, 1.45, places=9)
                for key in ("vt_err", "music_err", "vt_len_err", "vocal_err"):
                    self.assertLessEqual(abs(p.m[key]), TOL, key)
                # Primed admission cannot prove readiness before speech when
                # U > P + C (non-native media): the reason for direct admission.
                self.assertLess(p.m["deck_ready_lead_vs_speech"], 0.0)
                self.assertLessEqual(abs(p.m["measured_u"] - p.u), TOL)
                self.assertLessEqual(abs(p.m["clearance"] - M), TOL)

    def test_e04c_underlap_with_direct_admission_proves_readiness_before_speech(self):
        for rate, fmt in ((44100, "flac"), (44100, "flac"), (48000, "flac"), (48000, "wav")):
            with self.subTest(rate=rate, fmt=fmt):
                lab = self.lab(rate)
                p = self.transition(lab, f"E04c direct_underlap rate={rate} fmt={fmt}", out_len=1.6,
                                    out_ns=1.2, c=0.3, i=1.5, v=2.5, fmt=fmt, incoming="direct")
                self.assert_timing(p, "vt_err", "music_err", "vt_len_err", "vocal_err")
                self.assertLessEqual(abs(p.m["measured_u"] - p.u), TOL)
                self.assertLessEqual(abs(p.m["clearance"] - M), TOL)
                self.assertGreater(p.m["deck_ready_lead_vs_speech"], 0.3)

    def test_e04d_gated_hold_on_the_deck_mixer_starves_the_outgoing(self):
        """Rejected approach, recorded: holding the incoming behind a blocking
        probe leaves its deck-mixer pad empty; the non-live deck mixer then
        waits on it and the outgoing music stops."""
        lab = self.lab()
        p = self.transition(lab, "E04d gated_underlap (rejected)", out_len=1.6, out_ns=1.2,
                            c=0.3, i=1.5, v=2.5, fmt="flac", incoming="gated")
        self.assertLess(p.m["out_continuity"], 0.9)

    def test_e05_large_cue_in(self):
        for rate, fmt in ((44100, "wav"), (48000, "flac")):
            with self.subTest(rate=rate, fmt=fmt):
                lab = self.lab(rate)
                p = self.transition(lab, f"E05 cue_in_1.2 rate={rate} fmt={fmt}", out_len=1.6, out_ns=1.2,
                                    c=1.2, i=2.4, v=0.8, fmt=fmt)
                self.assertEqual(p.u, 0.0)
                self.assert_timing(p, "vt_err", "music_err", "vt_len_err", "vocal_err")

    def test_e06_missing_or_unusable_intro_until(self):
        for label, i in (("missing", None), ("before_cue", 0.1)):
            with self.subTest(case=label):
                lab = self.lab()
                p = self.transition(lab, f"E06 intro_until_{label}", out_len=1.6, out_ns=1.2,
                                    c=0.25, i=i, v=1.0, no_intro_len=1.0)
                self.assertAlmostEqual(p.u, 1.0 + M, places=9)
                self.assert_timing(p, "vt_err", "music_err", "vt_len_err")
                # Whole VT first, music M after it.
                self.assertLessEqual(abs(p.m["measured_u"] - (1.0 + M)), TOL)


class E07E08OutroTests(TransitionMixin, SimpleTestCase):
    def test_e07_outro_vt_past_outgoing_completion_holds_incoming(self):
        lab = self.lab()
        p = self.transition(lab, "E07 outro_past_outgoing_end", out_len=2.0, out_ns=1.8,
                            c=0.2, i=1.2, outro=(1.2, 2.0))
        self.assertGreater(p.o_end, p.t0 + P + 2.0)          # speech outlasts the outgoing
        self.assertAlmostEqual(p.a_i, p.o_end)              # D3: music when speech ends
        self.assert_timing(p, "outro_err", "outro_end_err", "music_err")
        out_end = p.trace.end(OUT, after=p.t0 + P + 0.5)
        report("E07 dry_outro", outgoing_end_err=out_end - (p.t0 + P + 2.0),
               dry_speech_seconds=p.o_end - out_end)

    def test_e08_outro_and_intro_with_separation(self):
        for rate, fmt in ((44100, "wav"), (48000, "flac")):
            with self.subTest(rate=rate, fmt=fmt):
                lab = self.lab(rate)
                p = self.transition(lab, f"E08 outro_intro_gap rate={rate} fmt={fmt}", out_len=2.4,
                                    out_ns=1.8, c=0.2, i=2.0, v=1.0, outro=(1.0, 1.4), gap=0.2, fmt=fmt)
                self.assert_timing(p, "outro_err", "outro_end_err", "vt_err", "music_err", "vt_len_err")
                self.assertLessEqual(abs(p.m["speech_gap"] - 0.2), TOL)


class E09E10CancellationTests(TransitionMixin, SimpleTestCase):
    def test_e09_eject_incoming_before_held_music_is_audible(self):
        """VT speaking, incoming music held back (U = 1.35 s): Eject the
        incoming deck. Its VT is cancelled (D9), the held music never airs,
        retirement is bounded and every pad is released."""
        lab = self.lab()
        before = lab.resources()
        record = {}

        def eject(lab, p):
            record["cancel_at"] = lab.now()
            began = time.monotonic()
            lab.engine._eject_deck("B", expected_log_item_id=2)
            record["eject_ms"] = (time.monotonic() - began) * 1000
            p.intro_branch.cancel("incoming_ejected", fade_ms=50)
        p = self.transition(lab, "E09 setup", out_len=1.6, out_ns=1.2, c=0.2, i=1.0, v=2.0,
                            actions=[(lambda p: p.s_v + 0.6, eject)], measure=False)
        self.assertAlmostEqual(p.u, 1.35, places=9)
        t = p.trace
        speech_end = t.end(SPEECH, after=p.s_v + 0.3)
        music = t.present(MUSIC, p.s_v, p.a_i + 2.0)
        pump(1.0)
        after = lab.resources()
        report("E09 eject_held_incoming", cancel_to_speech_end=speech_end - record["cancel_at"],
               held_music_seconds_aired=music, eject_main_loop_ms=record["eject_ms"],
               vt_teardown_main_loop_ms=p.intro_branch.teardown_ms, vt_null_ms=p.intro_branch.null_ms,
               deck_pads_before=before["deck_pads"], deck_pads_after=after["deck_pads"],
               fx_pads_before=before["fx_pads"], fx_pads_after=after["fx_pads"],
               children_before=before["children"], children_after=after["children"])
        self.assertEqual(music, 0.0)
        self.assertLessEqual(speech_end - record["cancel_at"], 0.050 + 0.020)
        self.assertEqual(after["deck_pads"], before["deck_pads"])
        self.assertEqual(after["fx_pads"], before["fx_pads"])
        self.assertEqual(after["children"], before["children"])

    def test_e09b_advancing_a_held_deck_by_reoffset(self):
        """Can held-back music be brought forward (VT cancelled, operator
        Start Next) by changing the pad offset of the held deck?"""
        for kind in ("primed_deck", "direct_scheduled", "gated_scheduled"):
            with self.subTest(kind=kind):
                lab = self.lab()
                path = lab.media("held", music_segments(0.2, 1.0))
                planned = lab.now() + 3.0
                if kind == "primed_deck":
                    deck = lab.create_deck("A", lab.item(30, path, duration=1.4, cue_in=0.2),
                                           offset_ns=int((planned - P - 0.2) * NS))
                    pad = deck.pipeline.get_static_pad("src")
                    lead = P + 0.2
                else:
                    branch = lab.branch(path, side="held", target_ns=int((planned - 0.2) * NS),
                                        duration=1.4, mixer=lab.engine.mixer,
                                        mode="scheduled" if kind == "direct_scheduled" else kind)
                    branch.start()
                    pad = branch.gain.get_static_pad("src")
                    lead = 0.2
                pump(0.8)
                new_target = lab.now() + 0.4
                if kind == "gated_scheduled":
                    self.assertTrue(branch.readmit(int((new_target - lead) * NS)))
                else:
                    pad.set_offset(int((new_target - lead) * NS))
                pump(planned - lab.now() + 1.5)
                trace = lab.trace()
                on = trace.onset(MUSIC, after=new_target - 0.5, before=planned + 1.0)
                report(f"E09b reoffset_{kind}", new_target_err=None if on is None else on - new_target,
                       original_target_err=None if on is None else on - planned)
                if kind == "gated_scheduled":
                    self.assertLessEqual(abs(on - new_target), TOL)
                else:
                    # An anchored (queued) buffer cannot be brought forward.
                    self.assertTrue(on is None or abs(on - new_target) > TOL)

    def test_e09d_readmission_by_regeneration(self):
        """Held music brought forward by retiring the held (never-audible)
        generation and opening a replacement scheduled generation at
        now + 0.10 s, while an outgoing deck keeps playing."""
        lab = self.lab()
        out = lab.media("regen-out", [(4.0, OUT)])
        music = lab.media("regen-in", music_segments(0.2, 1.2))
        t0 = lab.now() + 0.3
        lab.create_deck("A", lab.item(50, out, duration=4.0), offset_ns=int(t0 * NS))
        planned = t0 + P + 3.0
        held = lab.branch(music, side="incoming", target_ns=int((planned - 0.2) * NS), duration=1.6,
                          mixer=lab.engine.mixer)
        held.start()
        pump(1.2)
        record = {}

        def regenerate():
            record["at"] = lab.now()
            held.cancel("readmit", fade_ms=0)
            # Media position 0 admitted 0.10 s from now; Cue In 0.2 s later.
            new = lab.branch(music, side="incoming", target_ns=int((record["at"] + 0.10) * NS),
                             duration=1.6, mixer=lab.engine.mixer)
            new.start()
            record["new"] = new
            record["new_target"] = record["at"] + 0.10 + 0.2
        regenerate()
        pump(planned - lab.now() + 2.0)
        t = lab.trace()
        on = t.onset(MUSIC, after=record["at"] - 0.2, before=planned + 1.0)
        out_cont = t.present(OUT, t0 + P + 0.05, t0 + P + 3.95) / 3.9
        report("E09d regeneration", new_target_err=on - record["new_target"],
               command_to_music=on - record["at"], replacement_ready_after_open=(record["new"].ready_ns / NS - record["at"]),
               held_teardown_main_loop_ms=held.teardown_ms, outgoing_continuity=out_cont,
               original_plan_aired=t.present(MUSIC, planned - 0.05, planned + 0.05) > 0 and on > planned - 0.1)
        self.assertLessEqual(abs(on - record["new_target"]), TOL)
        self.assertGreaterEqual(out_cont, 0.97)

    def test_e09c_null_before_detach_blocks_on_a_held_branch(self):
        """Hazard check for the teardown order: NULL-first on a branch whose
        buffer is held in the mixer pad waits for the held admission time."""
        lab = self.lab()
        path = lab.media("hold-vt", vt_segments(0.6))
        branch = lab.branch(path, side="hazard", target_ns=lab.now_ns() + int(1.5 * NS), duration=0.6)
        branch.start()
        pump(0.3)
        result = {}

        def null_first():
            began = time.monotonic()
            for el in branch.elements:
                el.set_state(Gst.State.NULL)
            result["ms"] = (time.monotonic() - began) * 1000
        worker = threading.Thread(target=null_first, daemon=True)
        remaining = branch.target_ns / NS - lab.now()
        worker.start()
        deadline = time.monotonic() + 4.0
        while worker.is_alive() and time.monotonic() < deadline:
            pump(0.05)
        report("E09c null_first_on_held_branch", blocked_ms=result.get("ms"),
               hold_remaining_ms=remaining * 1000, still_blocked=worker.is_alive())
        branch.lab.owner.pop("hazard", None)
        branch.state = "ended"
        try:
            branch.gain.get_static_pad("src").unlink(branch.fx_pad)
            lab.engine.fx_submix.release_request_pad(branch.fx_pad)
        except Exception:
            pass

    def test_e10_late_cancel_during_audible_vt(self):
        lab = self.lab()
        record = {}

        def cancel(lab, p):
            record["at"] = lab.now()
            p.intro_branch.cancel("operator_start_next", fade_ms=50)
            now = lab.now()
            lab.duck_plan([(0.0, 1.0), (now, lab.engine.vt_duck_gain.get_property("volume") or self.DUCK),
                           (now + self.RAMP, 1.0)])
        p = self.transition(lab, "E10 setup", out_len=1.6, out_ns=1.2, c=0.2, i=3.0, v=2.0,
                            actions=[(lambda p: p.s_v + 0.8, cancel)], measure=False)
        t = p.trace
        speech_end = t.end(SPEECH, after=p.s_v + 0.3)
        ducked = t.level(MUSIC, record["at"] - 0.25, record["at"] - 0.05)
        released = t.level(MUSIC, record["at"] + self.RAMP + 0.05, record["at"] + self.RAMP + 0.4)
        report("E10 late_cancel", cancel_to_speech_end=speech_end - record["at"],
               music_level_ducked=ducked, music_level_after_release=released,
               vt_teardown_main_loop_ms=p.intro_branch.teardown_ms)
        self.assertLessEqual(speech_end - record["at"], 0.050 + 0.020)
        self.assertGreater(released, ducked * 1.6)


class E11E12FailureTests(TransitionMixin, SimpleTestCase):
    def _fail_hook(self, lab, p):
        """Speech-owned duck: a failed or cancelled VT removes its duck."""
        def watch():
            branch = p.intro_branch
            if branch is not None and branch.state in ("failed", "cancelled"):
                p.failed_at = lab.now()
                lab.duck_plan([(0.0, 1.0)])
                return False
            return True
        GLib.timeout_add(5, watch)

    def test_e11_invalid_or_failed_vt_branch(self):
        cases = {
            "corrupt_file": {},
            "stalled_preparation": {"vt_stall": 1.0},
            "overrun_validated_duration": {"vt_duration": 0.5},
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                lab = self.lab()
                vt_path = None
                if name == "corrupt_file":
                    vt_path = lab.root / "corrupt.wav"
                    vt_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk" + os.urandom(4096))
                v = 3.0 if name == "overrun_validated_duration" else 1.0
                p = self.transition(lab, f"E11 setup {name}", out_len=1.6, out_ns=1.2, c=0.2, i=3.5,
                                    v=v, vt_path=vt_path, measure=False,
                                    actions=[(lambda p: p.s_v - self.PREP + 0.01,
                                              lambda lab, p: self._fail_hook(lab, p))], **extra)
                t = p.trace
                branch = p.intro_branch
                music_on = t.onset(MUSIC, after=p.a_i - 0.6, before=p.a_i + 1.0)
                head = t.present(HEAD, p.s_v - 0.3, p.s_v + 1.0)
                speech = t.present(SPEECH, p.s_v - 0.3, p.s_v + 4.0)
                duck_floor = min(a for r, a in t.coarse(MUSIC) if p.a_i + 0.05 <= r < p.a_i + 1.0)
                report(f"E11 {name}", state=branch.state, reason=branch.reason,
                       failed_vs_admission=(branch.ended_ns / NS - p.s_v),
                       music_err=None if music_on is None else music_on - p.a_i,
                       head_seconds_aired=head, speech_seconds_aired=speech,
                       music_min_level_first_second=duck_floor)
                self.assertEqual(branch.state, "failed")
                self.assertLessEqual(abs(music_on - p.a_i), TOL)
                if name != "overrun_validated_duration":
                    self.assertEqual(head, 0.0)          # never a clipped head
                else:
                    # Completion watchdog: validated duration + 2.0 s.
                    self.assertLessEqual(abs(branch.ended_ns / NS - (p.s_v + 0.5 + 2.0)), 0.05)

    def test_e12_delayed_or_failed_incoming_deck(self):
        # (a) Deck preparation stalled 1.0 s past its planned admission.
        lab = self.lab()
        p = self.transition(lab, "E12 setup stalled_deck", out_len=2.4, out_ns=1.2, c=0.2, i=2.0, v=1.0,
                            deck_stall=1.0 + 0.45 + 0.2, measure=False)
        t = p.trace
        music_on = t.onset(MUSIC, after=p.a_i - 0.6, before=p.a_i + 3.0)
        out_gaps = t.present(OUT, p.t0 + P + 0.1, p.t0 + P + 2.3)
        vocal_planned = p.a_i + (2.0 - 0.2)
        vocal_on = t.onset(VOCAL, after=vocal_planned - 0.5, before=vocal_planned + 3.0)
        report("E12 stalled_deck", deck_ready_before_vt_start=p.deck_ready_before_vt_start,
               vocal_err=None if vocal_on is None else vocal_on - vocal_planned,
               deck_ready_vs_admission=(p.deck_ready.get("ready_ns", 0) / NS - (p.a_i - p.c)),
               music_err=None if music_on is None else music_on - p.a_i,
               music_head_lost=None if music_on is None else music_on - p.a_i,
               outgoing_present_of_2_2s=out_gaps)
        # Gating rule under test: a VT must not start for an unproven deck.
        self.assertFalse(p.deck_ready_before_vt_start)
        # (b) Deck could not be opened.
        lab2 = self.lab()
        missing = lab2.root / "missing.flac"
        deck = lab2.create_deck("B", lab2.item(40, missing, duration=2.0))
        pump(0.5)
        report("E12 failed_deck", deck_returned=deck is not None,
               start_failed=getattr(deck, "start_failed", None),
               exhausted_calls=len(lab2.h.on_log_exhausted_calls))


class E13LiveMicTests(TransitionMixin, SimpleTestCase):
    def test_e13_mic_live_during_speech_and_before_pending_speech(self):
        lab = self.lab(mic=True)
        record = {}
        mic_duck = 0.3

        def mic_on(lab, p):
            record["at"] = lab.now()
            lab.mic_gate.set_property("volume", 1.0)
            lab.engine.duck_gain.set_property("volume", mic_duck)     # mic duck (independent)
            p.intro_branch.cancel("mic_live", fade_ms=50)            # D6
            now = lab.now()
            lab.duck_plan([(0.0, 1.0), (now, self.DUCK), (now + self.RAMP, 1.0)])   # VT duck released

        p = self.transition(lab, "E13 setup", out_len=1.6, out_ns=1.2, c=0.2, i=3.5, v=2.0,
                            actions=[(lambda p: p.s_v + 0.8, mic_on)], measure=False)
        t = p.trace
        speech_end = t.end(SPEECH, after=p.s_v + 0.3)
        mic_on_at = t.onset(MIC, after=record["at"] - 0.2)
        music = t.level(MUSIC, record["at"] + 0.3, record["at"] + 0.8)
        report("E13 mic_during_speech", cancel_to_speech_end=speech_end - record["at"],
               mic_audible_after_command=mic_on_at - record["at"],
               music_level_under_mic=music, expected_mic_duck_level=0.4 * mic_duck,
               vt_speech_seconds_while_mic_live=t.present(SPEECH, record["at"] + 0.08, record["at"] + 2.0))
        self.assertLessEqual(speech_end - record["at"], 0.050 + 0.020)
        self.assertLessEqual(abs(music - 0.4 * mic_duck), 0.03)   # mic duck only; VT duck released
        lab.engine.duck_gain.set_property("volume", 1.0)

        # Pending speech: mic goes live before the VT is admitted.
        lab2 = self.lab(mic=True)

        def mic_before(lab, p):
            lab.mic_gate.set_property("volume", 1.0)
            p.intro_branch.cancel("mic_live", fade_ms=50)
            lab.duck_plan([(0.0, 1.0)])
        p2 = self.transition(lab2, "E13 setup pending", out_len=1.6, out_ns=1.2, c=0.2, i=2.0, v=1.0,
                             actions=[(lambda p: p.s_v - 0.15, mic_before)], measure=False)
        t2 = p2.trace
        report("E13 mic_before_speech", head_aired=t2.present(HEAD, p2.s_v - 0.3, p2.s_v + 1.5),
               speech_aired=t2.present(SPEECH, p2.s_v - 0.3, p2.s_v + 1.5),
               branch_state=p2.intro_branch.state)
        self.assertEqual(t2.present(HEAD, p2.s_v - 0.3, p2.s_v + 1.5), 0.0)


class E14RepeatedCycleTests(TransitionMixin, SimpleTestCase):
    CYCLES = 16

    def test_e14_repeated_transitions_no_drift_no_retained_resources(self):
        lab = self.lab()
        vt = {fmt: lab.media(f"cyc-vt-{fmt}", vt_segments(0.4), fmt=fmt) for fmt in ("wav", "flac")}
        music = {fmt: lab.media(f"cyc-in-{fmt}", music_segments(0.1, 0.7, after=0.3), fmt=fmt)
                 for fmt in ("wav", "flac")}
        pump(0.5)
        before = lab.resources()
        plans = []
        base = lab.now() + 0.8
        for n in range(self.CYCLES):
            fmt = "wav" if n % 2 == 0 else "flac"
            s_v = base + n * 1.3
            plans.append((s_v, fmt))
            item = lab.item(100 + n, music[fmt], duration=1.0, cue_in=0.1, intro_until=0.7)
            lab.at(s_v - 0.8, lambda slot, it, off: lab.create_deck(slot, it, offset_ns=off),
                   "AB"[n % 2], item, int((s_v - P - 0.1) * NS))
            lab.at(s_v - self.PREP, lambda path, s: lab.branch(path, side="intro", target_ns=int(s * NS),
                                                               duration=0.4).start(), vt[fmt], s_v)
        pump(base + self.CYCLES * 1.3 - lab.now() + 1.5)
        for branch in lab.branches:
            if getattr(branch, "null_thread", None) is not None:
                branch.null_thread.join(2.0)
        pump(1.0)
        after = lab.resources()
        trace = lab.trace()
        vt_errs, music_errs = [], []
        for s_v, _fmt in plans:
            vt_errs.append(trace.onset(HEAD, after=s_v - 0.3, before=s_v + 0.3) - s_v)
            music_errs.append(trace.onset(MUSIC, after=s_v - 0.3, before=s_v + 0.3) - s_v)
        sv, sm = stats(vt_errs), stats(music_errs)
        report("E14 cycles", n=self.CYCLES, vt_max_abs=sv["max_abs"], music_max_abs=sm["max_abs"],
               vt_first=vt_errs[0], vt_last=vt_errs[-1], music_first=music_errs[0], music_last=music_errs[-1],
               **{f"{k}_before": v for k, v in before.items()}, **{f"{k}_after": v for k, v in after.items()})
        self.assertLessEqual(sv["max_abs"], TOL)
        self.assertLessEqual(sm["max_abs"], TOL)
        for key in ("deck_pads", "fx_pads", "children"):
            self.assertEqual(after[key], before[key], key)
        self.assertLessEqual(after["threads"] - before["threads"], 2)
        self.assertLessEqual(after["fds"] - before["fds"], 4)


class R0144DeckTailTests(SimpleTestCase):
    """F14b (B0): the same EOS-then-release pattern on the r0144 deck path
    (_on_deck_eos_probed -> retirement) cuts the rendered end of a deck."""

    def test_r0144_deck_tail_is_cut(self):
        lab = Lab(self)
        lab.play()
        cuts = []
        for fmt in ("wav", "flac"):
            path = lab.media(f"deck-tail-{fmt}", [(1.2, OUT)], fmt=fmt)
            for rep_ in range(3):
                deck = lab.create_deck("AB"[rep_ % 2], lab.item(60 + rep_ + (10 if fmt == "flac" else 0), path,
                                                                duration=1.2))
                start = deck.lab["offset_ns"] / NS + P
                pump(2.0)
                trace = lab.trace()
                end = trace.end(OUT, after=start + 0.6)
                cuts.append(start + 1.2 - end)
                report(f"F14b r0144_deck_tail fmt={fmt}", rendered=end - start, file=1.2,
                       tail_cut=start + 1.2 - end)
        s = stats(cuts)
        report("F14b summary", n=s["n"], min_cut=s["min"], max_cut=s["max"], mean_cut=s["mean"])


class R0144TailTruncationTests(SimpleTestCase):
    """New finding F14 (B0): r0144 _fx_fire_completed tears a VT/FX branch
    down on its EOS pad probe. EOS is seen when the last buffer is QUEUED in
    the aggregator pad, so releasing the pad discards the not-yet-rendered
    tail. Measured on the unchanged production _vt_fire_file path."""

    def test_r0144_vt_fire_tail_is_cut(self):
        lab = Lab(self)
        lab.play()
        shortfalls = []
        for fmt in ("wav", "flac"):
            path = lab.media(f"r0144-tail-{fmt}", vt_segments(0.8), fmt=fmt)
            for _rep in range(3):
                fired = lab.now()
                lab.engine._vt_fire_file(str(path), 0.0, "intro")
                pump(1.4)
                trace = lab.trace()
                on = trace.onset(HEAD, after=fired - 0.05, before=fired + 0.5)
                end = trace.end(SPEECH, after=on + HEAD_S + 0.05)
                shortfalls.append(0.8 - (end - on))
                report(f"F14 r0144_vt_tail fmt={fmt}", heard=end - on, file=0.8, tail_cut=0.8 - (end - on))
        s = stats(shortfalls)
        report("F14 summary", n=s["n"], min_cut=s["min"], max_cut=s["max"], mean_cut=s["mean"])
        self.assertGreater(s["min"], 0.005)     # characterization: the r0144 tail is cut


class E12cBoundedPreparationTests(TransitionMixin, SimpleTestCase):
    """12c. A direct-admission incoming whose decode stalls leaves an empty
    deck-mixer pad, and the non-live deck mixer waits on it (F15). The
    readiness deadline (open + 100 ms) detaches it, which bounds the
    starvation; the control has no deadline."""

    def _run(self, mitigate):
        lab = self.lab()
        record = {}

        def open_watch(lab, p):
            def check():
                branch = p.incoming
                record["checked"] = True
                if branch.ready_ns is None:
                    record["detached_at"] = lab.now()
                    branch._finish("failed", "not_ready_after_open", fade_ms=0)
                    lab.duck_plan([(0.0, 1.0)])
                return False
            GLib.timeout_add(100, check)

        actions = [(lambda p: p.create_at + 0.001, open_watch)] if mitigate else []
        p = self.transition(lab, f"E12c setup mitigate={mitigate}", out_len=3.0, out_ns=2.0, c=0.2, i=2.0,
                            v=1.0, incoming="direct", deck_stall=1.0, actions=actions, measure=False)
        t = p.trace
        out_span = (p.t0 + P + 0.05, p.t0 + P + 2.95)
        cont = t.present(OUT, *out_span) / (out_span[1] - out_span[0])
        report(f"E12c stalled_direct_deck mitigate={mitigate}", outgoing_continuity=cont,
               outgoing_missing_seconds=(1.0 - cont) * (out_span[1] - out_span[0]),
               vt_head_aired=t.present(HEAD, p.s_v - 0.3, p.s_v + 1.0),
               incoming_state=p.incoming.state, detached_after_open=(
                   record["detached_at"] - p.incoming_created if "detached_at" in record else None))
        return cont, t, p

    def test_e12c_readiness_deadline_bounds_deck_mixer_starvation(self):
        cont_control, _t, _p = self._run(False)
        cont, t, p = self._run(True)
        span = 2.9
        self.assertLessEqual((1.0 - cont) * span, 0.15)          # bounded by the 100 ms deadline
        self.assertLess(cont_control, cont)
        self.assertEqual(t.present(HEAD, p.s_v - 0.3, p.s_v + 1.0), 0.0)   # no speech for an unviable deck
