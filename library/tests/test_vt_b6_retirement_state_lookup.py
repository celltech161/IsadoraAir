"""Roadmap 2.25 B6 -- legacy VT retirement, engine-state publication and
asynchronous VoiceTrack lookups (F13).

* VtLookupService in isolation (threads, no database): delayed answers,
  resolver exceptions, saturation, stale answers, de-duplication, worker
  death and bounded restart, bounded cache, stop.
* The engine on the real transition path with the THREADED worker (real
  GStreamer, rendered program PCM): a VoiceTrack added during the look-ahead
  airs; one removed after the claim never airs (a pre-claim answer never
  authorizes speech); a slow, failing or saturated worker never holds a
  transition or blocks the GLib thread.
* ``engine_state.json``'s ``vt_transition`` block (contract, JSON-safe, no
  paths, planned vs actual) at each stage, with a representative output.
* The r0144 phase machine, its ``_vt`` dictionary and the legacy duck stepper
  are gone; every former reader uses the occurrence-owned lifecycle.

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

import library.services.engine as eng_module
from library.services import hour_refill_causality, hour_refill_projection, vt_lookup, vt_transition
from library.tests._vt_integration_lab import (
    HEAD, MUSIC, OUT, TOL, fake_voicetrack, pump, report, wait_until,
)
from library.tests.test_vt_b4_intro_integration import RAMP, _IntroBase


class _Die(BaseException):
    """Escapes the resolver's ``except Exception`` and kills the worker."""


# ---------------------------------------------------------------------------
# VtLookupService (threads, no database)
# ---------------------------------------------------------------------------
class LookupServiceTests(SimpleTestCase):
    def service(self, resolve, **kwargs):
        notified = []
        service = vt_lookup.VtLookupService(resolve, notify=notified.append, **kwargs)
        self.addCleanup(service.stop, 2.0)
        return service, notified

    def test_requests_never_block_and_a_delayed_answer_arrives_once(self):
        gate = threading.Event()

        def slow(key, payload):
            gate.wait(2.0)
            return ("value", key)

        service, notified = self.service(slow)
        started = time.monotonic()
        self.assertTrue(service.request(("vt", "intro", 1)))
        self.assertTrue(service.request(("vt", "intro", 1)), "de-duplicated while in flight")
        elapsed = time.monotonic() - started
        self.assertIsNone(service.get(("vt", "intro", 1)))
        gate.set()
        self.assertTrue(wait_until(lambda: service.get(("vt", "intro", 1)) is not None, 2.0))
        report("B6-lookup-nonblocking", request_seconds=elapsed)
        self.assertLess(elapsed, 0.01)
        self.assertEqual(len(notified), 1)
        self.assertEqual(service.stats["deduplicated"], 1)

    def test_a_resolver_exception_is_an_unavailable_answer_not_a_dead_worker(self):
        calls = []

        def flaky(key, payload):
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("database unavailable")
            return "ok"

        service, _notified = self.service(flaky)
        service.request("a")
        self.assertTrue(wait_until(lambda: service.get("a") is not None, 2.0))
        self.assertFalse(service.get("a").ok)
        self.assertIn("database unavailable", service.get("a").error)
        service.request("b")
        self.assertTrue(wait_until(lambda: service.get("b") is not None, 2.0))
        self.assertTrue(service.get("b").ok)
        self.assertTrue(service.snapshot()["alive"])

    def test_saturation_refuses_without_blocking(self):
        gate = threading.Event()
        service, _notified = self.service(lambda key, payload: gate.wait(3.0), max_queue=2)
        results = []
        started = time.monotonic()
        for index in range(6):
            results.append(service.request(("k", index)))
        elapsed = time.monotonic() - started
        gate.set()
        report("B6-lookup-saturation", accepted=sum(results), refused=results.count(False), seconds=elapsed)
        self.assertIn(False, results)
        self.assertGreaterEqual(service.stats["saturated"], 1)
        self.assertLess(elapsed, 0.02)

    def test_an_older_answer_never_replaces_a_newer_one(self):
        delays = {}

        def resolve(key, payload):
            time.sleep(delays.pop(payload, 0.0))
            return payload

        service, _notified = self.service(resolve, inline=False)
        # Two workers' worth of ordering in one: the first request is slow and
        # its answer would arrive after a fresher one -- simulated by serving
        # the fresh request inline while the old one is still pending.
        delays["old"] = 0.0
        old_issued = time.monotonic()
        service._serve("k", "new", old_issued + 1.0)                # a fresher answer already cached
        service._serve("k", "old", old_issued)                      # the late, older answer
        self.assertEqual(service.get("k").value, "new")
        self.assertEqual(service.stats["stale_dropped"], 1)

    def test_fresh_after_forces_a_new_request_past_an_in_flight_one(self):
        gate = threading.Event()
        seen = []

        def resolve(key, payload):
            seen.append(payload)
            gate.wait(2.0)
            return payload

        service, _notified = self.service(resolve)
        service.request("k", "first")
        claim = time.monotonic() + 0.001
        time.sleep(0.002)
        self.assertTrue(service.request("k", "post-claim", fresh_after=claim))
        gate.set()
        self.assertTrue(wait_until(lambda: len(seen) == 2 and service.get("k") is not None
                                   and service.get("k").requested_mono >= claim, 2.0))
        self.assertEqual(service.get("k").value, "post-claim")

    def test_worker_death_restarts_a_bounded_number_of_times_then_reports_unavailable(self):
        def die(key, payload):
            raise _Die()

        service, _notified = self.service(die)
        for attempt in range(vt_lookup.MAX_RESTARTS + 2):
            service.request(("k", attempt))
            thread = service._thread
            if thread is not None:
                thread.join(1.0)
        report("B6-lookup-worker-death", restarts=service.restarts, unavailable=service.unavailable)
        self.assertEqual(service.restarts, vt_lookup.MAX_RESTARTS)
        self.assertTrue(service.unavailable)
        self.assertFalse(service.request("after"))

    def test_cache_is_bounded(self):
        service, _notified = self.service(lambda key, payload: key, inline=True, cache_size=4)
        for index in range(10):
            service.request(index)
        self.assertEqual(service.snapshot()["cached"], 4)
        self.assertIsNone(service.get(0))
        self.assertIsNotNone(service.get(9))


# ---------------------------------------------------------------------------
# The engine with the threaded worker (real transition path, rendered PCM)
# ---------------------------------------------------------------------------
class _ThreadedBase(_IntroBase):
    def threaded(self, lab, *, wrap=None):
        """Replace the lab engine's service by a THREADED one whose resolver
        is the engine's own (optionally wrapped: delays, failures)."""
        engine = lab.engine
        engine._vtt_lookup_inline = False
        resolve = engine._vtt_resolve_lookup if wrap is None else wrap(engine._vtt_resolve_lookup)
        service = vt_lookup.VtLookupService(resolve, notify=engine._vtt_lookup_notify)
        engine._vtt_lookup_service = service
        self.addCleanup(service.stop, 2.0)
        return service


class ThreadedLookupTests(_ThreadedBase, SimpleTestCase):
    def build_threaded(self, *, wrap=None, **kwargs):
        lab, out, inc, deck, zero = self.build(poll=False, **kwargs)
        self.threaded(lab, wrap=wrap)
        lab.poll()
        return lab, out, inc, deck, zero

    def test_voicetrack_added_during_the_lookahead_airs_at_the_segue(self):
        lab, out, inc, deck, zero = self.build_threaded(intro_vt=None)
        a_seg = zero + 8.0
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 4.0, 8.0))
        self.assertIsNone(lab.record(), "nothing to plan yet")
        path = lab.vt("b6-added", 1.2)
        lab.attach(inc, "intro", fake_voicetrack(path, 1.2))      # recorded now, before the claim
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 1.6, 8.0))
        head = lab.trace().onset(HEAD, after=a_seg - 1.0)
        report("B6-added-during-lookahead", onset_error=head - a_seg)
        self.assertLess(abs(head - a_seg), TOL)

    def test_voicetrack_removed_after_the_claim_never_airs(self):
        removed = {}

        def wrap(resolve):
            def resolver(key, payload):
                if key[0] == "vt" and removed.get("at") is not None:
                    return vt_transition.Recording(attached=False), None, 0.0
                return resolve(key, payload)
            return resolver

        lab, out, inc, deck, zero = self.build_threaded(wrap=wrap, in_cue=0.25, in_intro=1.75, intro_vt=3.0)
        engine = lab.engine
        original = engine._claim_playback_occurrence

        def claim_then_remove(item):
            if item.id == 2:
                removed["at"] = lab.now()            # the recording is deleted at the claim
            return original(item)
        engine._claim_playback_occurrence = claim_then_remove
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 2.5, 8.0))
        tr = lab.trace()
        record = lab.last_record()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        report("B6-removed-after-claim", intro_reason=record.intro_reason, music_vs_segue=music_on - a_seg)
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0), "a pre-claim answer never authorizes speech")
        self.assertIn(record.intro_reason, (vt_transition.Reason.RECORDING_UNPLAYABLE, "recording_unplayable"))
        self.assertLess(music_on - a_seg, 0.6, "held music re-admitted, no 1.65 s underlap for nothing")

    def test_a_slow_database_never_holds_the_transition(self):
        state = {"claimed": False}

        def wrap(resolve):
            def resolver(key, payload):
                if state["claimed"] and key[0] == "vt":
                    time.sleep(3.0)                  # the authoritative answer is too late
                return resolve(key, payload)
            return resolver

        lab, out, inc, deck, zero = self.build_threaded(wrap=wrap, intro_vt=1.2)
        engine = lab.engine
        original = engine._claim_playback_occurrence

        def mark(item):
            if item.id == 2:
                state["claimed"] = True
            return original(item)
        engine._claim_playback_occurrence = mark
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 1.5, 8.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        record = lab.last_record()
        report("B6-slow-db", intro_reason=record.intro_reason, music_vs_segue=music_on - a_seg,
               events=[name for _t, name, _d in record.events],
               lookups=lab.engine._vtt_lookups().snapshot())
        self.assertLess(abs(music_on - a_seg), TOL, "music lands at its plan")
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0))
        self.assertEqual(record.intro_reason, vt_transition.LOOKUP_UNAVAILABLE)

    def test_database_unavailable_plays_ordinarily_with_one_event(self):
        events = []

        def wrap(resolve):
            def resolver(key, payload):
                if key[0] == "vt":
                    raise RuntimeError("connection refused")
                return resolve(key, payload)
            return resolver

        lab, out, inc, deck, zero = self.build_threaded(wrap=wrap, intro_vt=1.2)
        # After the lab: the harness installs its own (silent) emit_event patch.
        with patch.object(eng_module, "emit_event", new=lambda **kw: events.append(kw)):
            self.wait_claims(lab, [1, 2])
            pump(2.0)
        tr = lab.trace()
        failing = [e for e in events if e.get("dedupe_key") == "engine|vt-lookup-failing"]
        report("B6-db-unavailable", events=len(failing), music_present=tr.present(MUSIC, zero + 8.0, lab.now()),
               all_events=[e.get("dedupe_key") for e in events], lookups=lab.engine._vtt_lookups().snapshot(),
               failures=lab.engine.__dict__.get("_vtt_lookup_failures"))
        self.assertGreater(tr.present(MUSIC, zero + 8.0, lab.now()), 0.5, "ordinary playout continues")
        self.assertIsNone(tr.onset(HEAD, after=zero))
        self.assertEqual(len(failing), 1, "one deduplicated operator event, not a flood")

    def test_saturated_worker_and_slow_resolver_never_block_the_glib_poll(self):
        gate = threading.Event()

        def wrap(resolve):
            def resolver(key, payload):
                gate.wait(5.0)
                return resolve(key, payload)
            return resolver

        lab, out, inc, deck, zero = self.build_threaded(wrap=wrap, intro_vt=1.2)
        self.addCleanup(gate.set)
        engine = lab.engine
        durations = []
        for _ in range(40):
            started = time.monotonic()
            engine._vtt_poll(deck)
            durations.append(time.monotonic() - started)
        report("B6-poll-under-saturation", max_poll_ms=max(durations) * 1000)
        self.assertLess(max(durations), 0.02, "the GLib callback never waits on the database")
        self.assertIsNone(lab.record(), "nothing planned without answers")


# ---------------------------------------------------------------------------
# engine_state.json "vt_transition"
# ---------------------------------------------------------------------------
class StatePublicationTests(_IntroBase, SimpleTestCase):
    def assert_safe(self, state):
        text = json.dumps(state)
        self.assertNotIn("/", text.replace("</", ""), "no filesystem paths published")
        for word in ("password", "secret", "filepath", "path\""):
            self.assertNotIn(word, text)
        return text

    def test_contract_through_one_long_intro_transition(self):
        lab, out, inc, deck, zero = self.build(in_cue=0.25, in_intro=1.75, intro_vt=3.0)
        engine = lab.engine
        a_seg = zero + 8.0
        idle = engine._vtt_state()
        self.assert_safe(idle)
        self.assertEqual((idle["version"], idle["active"], idle["phase"]), (1, False, "idle"))
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: (engine._vtt_state()["current"] or {}).get("incoming", {}).get("state")
                                   == "held", 2.0))
        held = engine._vtt_state()
        self.assertEqual(held["current"]["incoming"]["state"], "held")
        self.assertEqual(held["current"]["incoming"]["log_item_id"], 2)
        self.assertEqual(held["current"]["intro"]["status"], "prepared")
        self.assertIsNone(held["current"]["intro"]["started_at"], "planned is never published as aired")
        self.assertIsNotNone(held["current"]["intro"]["planned_start_at"])
        self.assertTrue(held["transport_busy"])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.5, 10.0))
        speaking = engine._vtt_state()
        self.assertEqual(speaking["current"]["stage"], "intro_speaking_held")
        self.assertIsNotNone(speaking["current"]["intro"]["started_at"])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 2.0, 6.0))
        over = engine._vtt_state()
        stage = (over["current"] or over["draining"] or {}).get("stage")
        self.assertIn(stage, ("intro_over_music", "draining"))
        self.assertTrue(wait_until(lambda: not engine._vtt_records(), 6.0))
        done = engine._vtt_state()
        text = self.assert_safe(done)
        self.assertEqual(done["recent"][0]["intro"], "completed")
        self.assertEqual(done["phase"], "idle")
        report("B6-state-sample", stage_held=held["current"]["stage"], stage_speaking=speaking["current"]["stage"],
               stage_over=stage, final_bytes=len(text))
        print("\nVT-STATE-SAMPLE " + json.dumps({"held": held, "speaking": speaking, "done": done}, sort_keys=True),
              flush=True)

    def test_manual_hold_and_operator_refusal_are_published(self):
        lab, out, inc, deck, zero = self.build(in_cue=0.25, in_intro=1.75, intro_vt=3.0)
        engine = lab.engine
        self.wait_claims(lab, [1, 2])
        held_deck = lab.deck_of(2)
        engine._seek_deck(held_deck.slot, 1.0)           # refused: not yet audible
        refusal = engine._vtt_state()["refusals"][-1]
        self.assertEqual((refusal["op"], refusal["reason"]), ("seek", "scheduled_for_vt_transition"))
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 8.4, 10.0))
        engine._set_manual_mode(True)
        state = engine._vtt_state()
        self.assertEqual(state["held_occurrence"]["log_item_id"], 2)
        self.assertEqual(state["held_occurrence"]["reason"], "manual")
        self.assert_safe(state)


# ---------------------------------------------------------------------------
# Legacy retirement and the consumers of the new lifecycle
# ---------------------------------------------------------------------------
LEGACY_METHODS = ("_vt_maybe_enter", "_vt_on_fire_eos", "_vt_handle_outgoing_ended", "_vt_current",
                  "_vt_advance_to_intro_gap", "_vt_start_intro_phase", "_vt_start_incoming_now",
                  "_vt_complete", "_vt_abort", "_vt_ramp_duck_to_db", "_vt_fire_file", "_vtt_publish")


def _busy_record():
    record = eng_module.VtTransitionRecord(41, None, duck_db=-6.0, ramp_s=0.3, gap_s=0.2)
    record.owns_start = True
    return record


class LegacyRetirementTests(SimpleTestCase):
    def test_no_callable_legacy_transition_path_remains(self):
        source = inspect.getsource(eng_module)
        for name in LEGACY_METHODS:
            self.assertFalse(hasattr(eng_module.PlaybackEngine, name), name)
        for needle in ("self._vt.", "self._vt[", "self._vt =", "_vt_lock", "vt_kind", "busy_voice_track"):
            self.assertNotIn(needle, source, needle)
        self.assertNotIn("_vt_on_fire_eos", inspect.getsource(eng_module.PlaybackEngine._fx_fire_completed))
        self.assertTrue(hasattr(eng_module.PlaybackEngine, "_vt_resolve_audio"), "media resolution is kept")

    def test_former_readers_use_the_occurrence_owned_lifecycle(self):
        engine = object.__new__(eng_module.PlaybackEngine)
        self.assertEqual(engine._vtt_observed_phase(), "idle")
        self.assertFalse(engine._vtt_transport_busy())
        engine._vtt = _busy_record()
        self.assertTrue(engine._vtt_transport_busy())
        self.assertEqual(engine._vtt_observed_phase(), "commit_pending")
        self.assertEqual(hour_refill_projection._engine_vt_phase(engine), "commit_pending")
        self.assertEqual(hour_refill_causality._engine_vt_phase(engine), "commit_pending")
        engine._vtt = None
        self.assertEqual(hour_refill_projection._engine_vt_phase(engine), "idle")
        broken = SimpleNamespace()
        self.assertEqual(hour_refill_projection._engine_vt_phase(broken), "idle", "not an engine: no VT")
        for reader in ("_on_deck_teardown_completed", "_eject_deck", "_start_next_command"):
            method = getattr(eng_module.PlaybackEngine, reader, None)
            if method is not None:
                self.assertNotIn("_vt.get", inspect.getsource(method), reader)
        self.assertIn("_vtt_transport_busy", inspect.getsource(eng_module.PlaybackEngine._eject_deck))


class OneDuckControllerTests(_IntroBase, SimpleTestCase):
    def test_vt_duck_gain_has_exactly_one_controller_and_no_stepper(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.2)
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > zero + 10.0, 8.0))
        gain = lab.engine.vt_duck_gain
        bindings = [b for b in gain.control_bindings]
        self.assertEqual(len(bindings), 1, "one control binding: the speech-owned curve")
        self.assertFalse(hasattr(lab.engine, "_vt_ramp_serial"), "the legacy stepper never ran")
        tr = lab.trace()
        self.assertLess(tr.level(OUT, zero + 8.0 - RAMP - 0.15, zero + 8.0 - RAMP - 0.02), 1.0)
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)
