"""Roadmap 2.27 Phase B: pure projection, placement and search.

Every test here is a SimpleTestCase over synthetic snapshots: Django refuses
any database query from a SimpleTestCase, so these also prove that the
projection, placement and best-fit search are pure. Arithmetic cases pass
allowance=0 so the expected seconds are exact; the default one-poll-tick
allowance is covered separately."""
import random
from dataclasses import replace
from datetime import date, datetime, timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from library.services import hour_refill_projection as proj
from library.services.hour_refill_causality import (
    CAUSE_OPERATOR_EJECT,
    CAUSE_REQUEST_SUBSTITUTION,
    CauseRecord,
)
from library.services.hour_refill_projection import (
    BOUNDARY_ORDINARY,
    BOUNDARY_PARTIAL,
    BOUNDARY_UNKNOWN,
    BoundaryProof,
    DeckState,
    ItemTiming,
    PlaybackWindowSnapshot,
    element_span,
    project_refill,
)
from library.services.hour_refill_proposal import (
    Candidate,
    FitFrame,
    PROPOSAL_TTL_SECONDS,
    RefillProposal,
    ProposedItem,
    RequestContext,
    assess_placement,
    last_remaining_music,
    proposal_staleness,
    search_best_fit,
)

LOG = 501
DAY = date(2027, 6, 1)
GOLD, RECURRENT, PSA, LEGAL = 11, 12, 21, 22
NO_REQUESTS = RequestContext(frozenset(), frozenset(), frozenset())


def at(hour, minute, second=0, day=DAY):
    return timezone.make_aware(datetime.combine(day, datetime.min.time()).replace(
        hour=hour, minute=minute, second=second), timezone.get_current_timezone())


def item(item_id, seconds=240.0, *, kind="music", category=GOLD, cue_in=0.0, next_start=None,
         duration=None, track=None, claimed=False, played=False, log=LOG, position=None):
    return ItemTiming(
        log_item_id=item_id, playlist_log_id=log, position=item_id if position is None else position,
        track_id=1000 + item_id if track is None else track, category_id=category, category_kind=kind,
        origin="generated_schedule", cue_in=cue_in,
        next_start=seconds if next_start is None else next_start,
        duration=seconds if duration is None else duration, claimed=claimed, played=played,
    )


def deck(occurrence, position, slot="A", **extra):
    values = dict(slot=slot, generation=7, item=occurrence, position_seconds=position, live=True,
                  paused=False, on_air=True, continuation=False)
    values.update(extra)
    return DeckState(**values)


def snapshot(queue, cursor, decks, *, now=None, forced=(), log_hour=14, log_date=DAY, **flags):
    return PlaybackWindowSnapshot(
        captured_at=now or at(14, 40), engine_instance="eng-test", active_log_id=LOG,
        active_log_date=log_date, active_log_hour=log_hour, cursor=cursor, queue=tuple(queue),
        forced=tuple(forced), decks=tuple(decks), poison_skip=frozenset(), **flags,
    )


def boundary(instant=None, kind=BOUNDARY_ORDINARY, source="schedule_resolution"):
    return BoundaryProof(kind, instant or at(15, 0), source, DAY, 15)


def eject(cause_id, log_item_id, *, category=GOLD, shortening=120.0, minute=30, log=LOG, hour=14):
    return CauseRecord(
        id=cause_id, source_key=f"operator_eject:eng:A:g{cause_id}:li{log_item_id}",
        cause_type=CAUSE_OPERATOR_EJECT, occurred_at=at(14, minute), log_id=log, log_date=DAY,
        log_hour=hour, log_item_id=log_item_id, log_item_position=log_item_id,
        scheduled_category_id=category, scheduled_category_code=f"C{category}",
        scheduled_category_kind_code="music", original_track_id=1000 + log_item_id,
        original_effective_airtime_seconds=240.0, replacement_track_id=None,
        replacement_effective_airtime_seconds=None, observed_shortening_seconds=shortening,
    )


def swap(cause_id, log_item_id, replacement_track, *, category=GOLD, shortening=90.0, minute=31, log=LOG,
         hour=14):
    return CauseRecord(
        id=cause_id, source_key=f"request_substitution:li{log_item_id}:req{cause_id}:t9-{replacement_track}",
        cause_type=CAUSE_REQUEST_SUBSTITUTION, occurred_at=at(14, minute), log_id=log, log_date=DAY,
        log_hour=hour, log_item_id=log_item_id, log_item_position=log_item_id,
        scheduled_category_id=category, scheduled_category_code=f"C{category}",
        scheduled_category_kind_code="music", original_track_id=9, original_effective_airtime_seconds=300.0,
        replacement_track_id=replacement_track, replacement_effective_airtime_seconds=210.0,
        observed_shortening_seconds=shortening, song_request_id=cause_id,
    )


def hour(future_seconds, *, deck_seconds=240.0, deck_position=10.0, extra_future=()):
    """Window 14:40 -> 15:00 = 1200 s. Played item 1, ejected item 2, item 3
    on the deck, then future items 4.. summing to `future_seconds`."""
    played = [item(1, played=True, claimed=True), item(2, played=True, claimed=True)]
    on_deck = item(3, deck_seconds, played=True, claimed=True)
    future = [item(4 + i, s) for i, s in enumerate(future_seconds)] + list(extra_future)
    return snapshot(played + [on_deck] + future, 3, [deck(on_deck, deck_position)])


class ElementSpanTests(SimpleTestCase):
    def test_ordinary_queued_span_is_effective_airtime(self):
        self.assertEqual(element_span(2.0, 200.0, 1.0, allowance=0), 198.0)

    def test_trigger_clamp_adds_the_successor_cue_in(self):
        # trigger 8 - 3 = 5 < 10 -> clamped to 8; successor audible at 11.
        self.assertEqual(element_span(0.0, 8.0, 3.0, allowance=0), 11.0)

    def test_poll_floor_for_very_short_items(self):
        self.assertEqual(element_span(0.0, 3.0, None, allowance=0), 5.0)

    def test_live_deck_counts_from_position_and_never_before_it(self):
        self.assertEqual(element_span(0.0, 200.0, None, position=50.0, allowance=0), 150.0)
        # Already past its trigger with nothing queued: a successor starts at once.
        self.assertEqual(element_span(0.0, 200.0, 5.0, position=199.0, allowance=0), 5.0)

    def test_allowance_is_one_poll_tick_per_transition(self):
        self.assertEqual(element_span(0.0, 100.0, None), 100.0 + proj.TRANSITION_ALLOWANCE_SECONDS)


class NetDeficitTests(SimpleTestCase):
    def project(self, snap, causes, **kwargs):
        return project_refill(snap, causes=causes, boundary=kwargs.pop("boundary", boundary()),
                              allowance=kwargs.pop("allowance", 0.0))

    # Case 1 -- single Eject, partially aired.
    def test_single_eject_counts_live_runway_once_and_uses_net(self):
        p = self.project(hour([290, 290, 290]), [eject(1, 2, shortening=200.0)])
        self.assertEqual(p.outcome, proj.OUTCOME_ELIGIBLE)
        self.assertEqual(p.window_seconds, 1200.0)
        self.assertEqual(p.runway.deck_remaining_seconds, 230.0)
        self.assertEqual(p.runway.total_seconds, 230.0 + 870.0)
        self.assertEqual(p.net_shortage_seconds, 100.0)  # not the cause's 200 s
        self.assertEqual(p.observed_shortening_seconds, 200.0)
        self.assertEqual([e[0] for e in p.runway.elements], [3, 4, 5, 6])
        self.assertEqual((p.threshold_seconds, p.threshold_basis), (30.0, "operator_eject_only_provisional"))

    def test_eject_only_gate_is_strictly_greater_than_30(self):
        at_30 = self.project(hour([290, 290, 360]), [eject(1, 2)])
        self.assertEqual((at_30.net_shortage_seconds, at_30.outcome), (30.0, proj.OUTCOME_BELOW_THRESHOLD))
        above = self.project(hour([290, 290, 359.999]), [eject(1, 2)])
        self.assertEqual(above.outcome, proj.OUTCOME_ELIGIBLE)

    # Case 2 -- several Ejects plus a web swap: one window, one net, 60 s gate.
    def test_mixed_causes_share_one_window_in_causal_order(self):
        snap = hour([290, 290, 250])
        snap = replace(snap, queue=snap.queue[:4] + (replace(snap.queue[4], track_id=77),) + snap.queue[5:])
        causes = [swap(3, 5, 77, minute=35), eject(2, 2, minute=20), eject(1, 1, category=RECURRENT, minute=10)]
        p = self.project(snap, causes)
        self.assertEqual([c.cause_id for c in p.qualified_causes], [1, 2, 3])
        self.assertEqual(p.net_shortage_seconds, 140.0)
        self.assertEqual(p.observed_shortening_seconds, 330.0)  # never the deficit
        self.assertEqual((p.threshold_seconds, p.threshold_basis), (60.0, "request_substitution_present"))
        self.assertEqual(p.outcome, proj.OUTCOME_ELIGIBLE)

    # Case 3 -- the strict web rule applies to the NET deficit.
    def web(self, future_total):
        snap = hour([290, 290, future_total - 580])
        snap = replace(snap, queue=snap.queue[:4] + (replace(snap.queue[4], track_id=77),) + snap.queue[5:])
        return self.project(snap, [swap(1, 5, 77)])

    def test_web_exactly_60_net_is_not_eligible(self):
        p = self.web(910.0)
        self.assertEqual((p.net_shortage_seconds, p.outcome), (60.0, proj.OUTCOME_BELOW_THRESHOLD))

    def test_web_just_over_60_net_is_eligible(self):
        self.assertEqual(self.web(909.999).outcome, proj.OUTCOME_ELIGIBLE)
        self.assertEqual(self.web(909.0).net_shortage_seconds, 61.0)
        self.assertEqual(self.web(909.0).outcome, proj.OUTCOME_ELIGIBLE)

    def test_90s_shortening_offset_by_50s_overrun_is_40s_net_no_music(self):
        # Before the swap the hour ran 50 s past the boundary (230 + 1020).
        # The 5:00 -> 3:30 swap removed 90 s: 230 + 930 = 1160 -> 40 s net.
        p = self.web(930.0)
        self.assertEqual(p.net_shortage_seconds, 40.0)
        self.assertEqual(p.qualified_causes[0].observed_shortening_seconds, 90.0)
        self.assertEqual(p.outcome, proj.OUTCOME_BELOW_THRESHOLD)

    # Case 4 -- the Grateful Dead long final segment.
    def test_long_final_segment_on_deck_is_no_artificial_deficit(self):
        played = [item(1, played=True, claimed=True), item(2, played=True, claimed=True)]
        part2 = item(3, 1620.0, played=True, claimed=True)
        snap = snapshot(played + [part2], 3, [deck(part2, 30.0)], now=at(14, 33, 30))
        p = self.project(snap, [eject(1, 2)])
        self.assertEqual(p.runway.deck_remaining_seconds, 1590.0)
        self.assertEqual(p.window_seconds, 1590.0)
        self.assertEqual(p.net_shortage_seconds, 0.0)
        self.assertEqual(p.outcome, proj.OUTCOME_ALREADY_SATISFIED)

    def test_long_final_segment_without_cause_still_reports_zero(self):
        played = [item(1, played=True, claimed=True)]
        part2 = item(3, 1700.0, played=True, claimed=True)
        p = self.project(snapshot(played + [part2], 2, [deck(part2, 30.0)], now=at(14, 33, 30)), [])
        self.assertEqual((p.net_shortage_seconds, p.overfill_seconds), (0.0, 80.0))
        self.assertEqual(p.outcome, proj.OUTCOME_NO_QUALIFYING_CAUSE)

    # Case 6 -- partial-hour takeover earlier than the nominal top of hour.
    def continuation(self, now, takeover):
        played = [item(1, played=True, claimed=True)]
        on_deck = item(2, 640.0, played=True, claimed=True)
        snap = snapshot(played + [on_deck, item(3, 280.0)], 2, [deck(on_deck, 40.0)], now=now, log_hour=13)
        return self.project(snap, [eject(1, 1, hour=13)],
                            boundary=boundary(takeover, kind=BOUNDARY_PARTIAL, source="approved_next_log"))

    def test_continuation_projects_to_the_partial_takeover_not_the_top_of_hour(self):
        p = self.continuation(at(14, 5), at(14, 20))
        self.assertEqual(p.window_seconds, 900.0)
        self.assertEqual(p.net_shortage_seconds, 20.0)
        self.assertEqual(p.outcome, proj.OUTCOME_BELOW_THRESHOLD)
        self.assertEqual(p.boundary.handoff, "natural")  # never a hard cut

    def test_continuation_after_its_takeover_is_wrong_hour(self):
        self.assertEqual(self.continuation(at(14, 25), at(14, 20)).outcome, proj.OUTCOME_WRONG_HOUR)

    def test_ordinary_boundary_cannot_own_a_continuation(self):
        snap = replace(hour([290]), active_log_hour=13)
        self.assertEqual(self.project(snap, [eject(1, 2, hour=13)]).outcome, proj.OUTCOME_WRONG_HOUR)

    def test_unproven_boundary_abstains(self):
        unknown = BoundaryProof(BOUNDARY_UNKNOWN, None, "none", DAY, 15, reason="next_hour_unscheduled")
        p = self.project(hour([290]), [eject(1, 2)], boundary=unknown)
        self.assertEqual((p.outcome, p.reasons), (proj.OUTCOME_NO_SAFE_BOUNDARY, ("next_hour_unscheduled",)))
        self.assertIsNone(p.net_shortage_seconds)

    def test_imminent_takeover_abstains(self):
        p = self.project(replace(hour([]), captured_at=at(14, 59, 0)), [eject(1, 2)])
        self.assertEqual((p.outcome, p.reasons), (proj.OUTCOME_UNSAFE_WINDOW, ("imminent_takeover",)))

    # Case 7 -- claimed/prepared, dedication pair, overlap, clamp.
    def test_deck_item_still_in_the_queue_is_counted_once(self):
        snap = hour([290, 290])
        snap = replace(snap, cursor=2)  # the prepared deck item is still at the cursor
        p = self.project(snap, [eject(1, 2)])
        self.assertEqual([e[0] for e in p.runway.elements], [3, 4, 5])

    def test_claimed_not_yet_audible_deck_counts_from_its_position(self):
        on_deck = item(3, 240.0, cue_in=4.0, claimed=True)
        snap = snapshot([item(1, played=True), item(2, played=True), on_deck, item(4, 290.0)], 3,
                        [deck(on_deck, 0.5, on_air=False)])
        p = self.project(snap, [eject(1, 2)])
        self.assertEqual(p.runway.deck_remaining_seconds, 239.5)  # pre-roll is wall-clock runway
        self.assertEqual(p.runway.total_seconds, 239.5 + 290.0)

    def test_forced_dedication_song_is_not_counted_twice(self):
        intro = item(9, 8.0, category=31, kind="talk")
        song = item(4, 200.0, cue_in=1.0)
        queue = [item(1, played=True), item(2, played=True), intro, song, item(5, 300.0)]
        snap = snapshot(queue, 3, [deck(intro, 6.0)], forced=[song])
        p = self.project(snap, [eject(1, 2)])
        self.assertEqual([e[0] for e in p.runway.elements], [9, 4, 5])
        # intro: trigger 8 - 1 = 7 < 10 -> clamped to 8; song audible at 9.
        self.assertEqual(p.runway.elements[0][2], 3.0)

    def test_trigger_clamp_is_counted_in_the_runway(self):
        short_id = item(4, 6.0, kind="imaging", category=41)
        song = item(5, 200.0, cue_in=2.0)
        p = self.project(hour([], extra_future=[short_id, song]), [eject(1, 2)])
        # trigger 6 - 2 = 4 < 10 -> 6 (>= the 5 s poll floor); song audible at 8.
        self.assertEqual(p.runway.elements[1], (4, "queue", 8.0))

    def test_two_deck_overlap_is_blocked(self):
        snap = hour([290])
        snap = replace(snap, decks=snap.decks + (deck(item(4, 290.0), 2.0, slot="B"),))
        p = self.project(snap, [eject(1, 2)])
        self.assertEqual((p.outcome, p.reasons), (proj.OUTCOME_BLOCKED_TRANSPORT, ("overlap",)))

    def test_skipped_ghost_and_poison_items_add_no_runway(self):
        ghost = replace(item(4, 290.0), track_id=None)
        snap = replace(hour([], extra_future=[ghost, item(5, 290.0)]), poison_skip=frozenset({(LOG, 5)}))
        p = self.project(snap, [eject(1, 2)])
        self.assertEqual(p.runway.skipped, ((4, "track_deleted"), (5, "poison_guarded")))
        self.assertEqual(p.runway.total_seconds, 230.0)

    def test_default_allowance_is_an_upper_bound(self):
        p = project_refill(hour([290, 290, 290]), causes=[eject(1, 2)], boundary=boundary())
        self.assertEqual(p.runway.allowance_seconds, 4 * proj.TRANSITION_ALLOWANCE_SECONDS)
        self.assertEqual(p.net_shortage_seconds, 99.0)

    # Case 8 -- cause scoping.
    def test_cause_scoping_and_exclusions(self):
        snap = hour([290, 290, 290])
        snap = replace(snap, queue=snap.queue[:4] + (replace(snap.queue[4], track_id=77),) + snap.queue[5:])
        causes = [
            eject(1, 2),
            replace(eject(2, 1), source_key=eject(1, 2).source_key),           # duplicate key
            replace(eject(3, 2), source_key="operator_eject:other"),           # same occurrence again
            eject(4, 1, hour=15),                                              # future hour
            eject(5, 1, log=999),                                              # rebuilt/stale log
            eject(6, 404),                                                     # LogItem deleted
            eject(7, 5),                                                       # still ahead in the queue
            swap(8, 5, 78),                                                    # slot changed since
            replace(eject(9, 1), cause_type="start_next"),                     # not a v1 type
            replace(eject(10, 3), scheduled_category_kind_code="spot"),        # not a music slot
        ]
        p = self.project(snap, causes)
        self.assertEqual([c.cause_id for c in p.qualified_causes], [1])
        reasons = dict(p.excluded_causes)
        self.assertEqual(reasons["operator_eject:other"], "duplicate_occurrence_cause")
        self.assertEqual(reasons[eject(4, 1, hour=15).source_key], "other_hour")
        self.assertEqual(reasons[eject(5, 1, log=999).source_key], "stale_log")
        self.assertEqual(reasons[eject(6, 404).source_key], "missing_occurrence")
        self.assertEqual(reasons[eject(7, 5).source_key], "occurrence_still_pending")
        self.assertEqual(reasons[swap(8, 5, 78).source_key], "superseded")
        self.assertIn("unknown_cause_type", reasons.values())
        self.assertIn("not_music_slot", reasons.values())
        self.assertIn("duplicate_source_key", reasons.values())

    def test_no_causes_means_no_proposal_but_the_net_is_reported(self):
        p = self.project(hour([290, 290, 290]), [])
        self.assertEqual(p.outcome, proj.OUTCOME_NO_QUALIFYING_CAUSE)
        self.assertEqual(p.net_shortage_seconds, 100.0)
        self.assertIn("best effort", p.notes[0])

    # Case 14 -- on-air ownership.
    def test_ownership_and_transition_states_block(self):
        cases = {
            "manual_mode": dict(manual_mode=True), "remote_dj": dict(remote_dj=True),
            "mic_live": dict(mic_live=True), "voice_track": dict(vt_phase="outro"),
            "start_next": dict(start_next_active=True), "transition_pending": dict(next_triggered=True),
            "resume_pending": dict(resume_pending=True), "live_fill_pending": dict(live_fill_in_progress=True),
        }
        for reason, flags in cases.items():
            p = self.project(replace(hour([290]), **flags), [eject(1, 2)])
            self.assertEqual((p.outcome, p.reasons), (proj.OUTCOME_BLOCKED_TRANSPORT, (reason,)), reason)

    def test_paused_or_missing_deck_abstains(self):
        snap = hour([290])
        paused = replace(snap, decks=(replace(snap.decks[0], paused=True, live=False),))
        self.assertEqual(self.project(paused, [eject(1, 2)]).reasons, ("paused_deck",))
        idle = replace(snap, decks=())
        self.assertEqual(self.project(idle, [eject(1, 2)]).outcome, proj.OUTCOME_UNSAFE_WINDOW)

    # Case 15 -- clock, DST and unreliable data.
    def test_unknown_or_implausible_position_abstains(self):
        snap = hour([290])
        for position, reason in ((None, "position_unknown"), (-1.0, "position_implausible"),
                                 (260.0, "position_implausible")):
            p = self.project(replace(snap, decks=(replace(snap.decks[0], position_seconds=position),)),
                             [eject(1, 2)])
            self.assertEqual((p.outcome, p.reasons), (proj.OUTCOME_UNSAFE_WINDOW, (reason,)))

    def test_unreliable_durations_abstain_instead_of_fabricating_a_deficit(self):
        for bad in (item(4, 0.0), replace(item(4, 200.0), next_start=None, duration=None),
                    item(4, 200.0, cue_in=-1.0), item(4, 200.0, cue_in=250.0)):
            p = self.project(hour([], extra_future=[bad]), [eject(1, 2)])
            self.assertEqual(p.outcome, proj.OUTCOME_UNSAFE_WINDOW)
            self.assertIsNone(p.net_shortage_seconds)

    def test_fall_back_hour_abstains(self):
        day = date(2027, 11, 7)  # America/Chicago leaves CDT at 02:00
        snap = replace(hour([290]), active_log_date=day, active_log_hour=1, captured_at=at(1, 40, day=day))
        p = self.project(snap, [replace(eject(1, 2, hour=1), log_date=day)],
                         boundary=BoundaryProof(BOUNDARY_ORDINARY, at(2, 0, day=day), "schedule_resolution"))
        self.assertEqual((p.outcome, p.reasons), (proj.OUTCOME_UNSAFE_WINDOW, ("dst_utc_offset_change",)))

    def test_spring_forward_missing_hour_abstains(self):
        day = date(2027, 3, 14)
        snap = replace(hour([290]), active_log_date=day, active_log_hour=1, captured_at=at(1, 40, day=day))
        p = self.project(snap, [replace(eject(1, 2, hour=1), log_date=day)],
                         boundary=BoundaryProof(BOUNDARY_ORDINARY, at(3, 0, day=day), "schedule_resolution"))
        self.assertEqual((p.outcome, p.reasons), (proj.OUTCOME_UNSAFE_WINDOW, ("dst_hour_does_not_exist",)))

    def test_invalid_snapshots(self):
        snap = hour([290])
        cases = {
            "capture": replace(snap, capture_errors=("queue[3]:track_not_loaded",)),
            "naive": replace(snap, captured_at=datetime(2027, 6, 1, 14, 40)),
            "cursor": replace(snap, cursor=99),
            "mixed": replace(snap, queue=snap.queue + (item(9, log=777),)),
        }
        for name, bad in cases.items():
            self.assertEqual(self.project(bad, [eject(1, 2)]).outcome, proj.OUTCOME_INVALID_SNAPSHOT, name)


class SnapshotDigestTests(SimpleTestCase):
    # Case 12 -- same-count changes are visible.
    def test_reorder_and_same_count_track_change_change_the_digest(self):
        snap = hour([290, 280])
        reordered = replace(snap, queue=snap.queue[:3] + (snap.queue[4], snap.queue[3]))
        retracked = replace(snap, queue=snap.queue[:4] + (replace(snap.queue[4], track_id=4242),))
        claimed = replace(snap, queue=snap.queue[:4] + (replace(snap.queue[4], claimed=True),))
        digests = {s.digest() for s in (snap, reordered, retracked, claimed)}
        self.assertEqual(len(digests), 4)

    def test_playhead_and_clock_do_not_change_the_digest(self):
        snap = hour([290])
        later = replace(snap, captured_at=at(14, 41), decks=(replace(snap.decks[0], position_seconds=70.0),))
        self.assertEqual(snap.digest(), later.digest())


class PlacementTests(SimpleTestCase):
    # Case 5 -- protected closing rows.
    def closer_hour(self, **closer):
        future = [item(4, 290.0), item(5, 60.0, kind="spot", category=PSA, **closer),
                  item(6, 10.0, kind="legal_id", category=LEGAL)]
        on_deck = item(3, played=True)
        return snapshot([item(1, played=True), item(2, played=True), on_deck] + future, 3, [deck(on_deck, 10)])

    def test_final_music_row_is_a_tail_append(self):
        placement = assess_placement(hour([290, 280]), NO_REQUESTS)
        self.assertEqual((placement.mode, placement.after_log_item_id, placement.start_position),
                         ("tail_append", 5, 6))

    def test_closing_block_gives_an_insert_before_anchor_and_keeps_its_order(self):
        snap = self.closer_hour()
        placement = assess_placement(snap, NO_REQUESTS)
        self.assertEqual((placement.mode, placement.anchor_log_item_id), ("insert_before", 5))
        self.assertIn("Phase C", placement.requires)
        self.assertEqual([it.log_item_id for it in snap.queue], [1, 2, 3, 4, 5, 6])  # untouched

    def test_unclear_anchors_are_refused(self):
        self.assertEqual(assess_placement(self.closer_hour(claimed=True), NO_REQUESTS).reason, "closer_claimed")
        next_up = replace(self.closer_hour(), cursor=4)
        self.assertEqual(assess_placement(next_up, NO_REQUESTS).reason, "anchor_next_up")
        requests = RequestContext(frozenset(), frozenset({5}), frozenset())
        self.assertEqual(assess_placement(self.closer_hour(), requests).reason, "closer_request_slot")
        intro = RequestContext(frozenset(), frozenset(), frozenset({4}))
        self.assertEqual(assess_placement(self.closer_hour(), intro).reason, "intro_adjacency")
        unknown = self.closer_hour()
        unknown = replace(unknown, queue=unknown.queue[:5] + (replace(unknown.queue[5], category_kind=None),))
        self.assertEqual(assess_placement(unknown, NO_REQUESTS).mode, None)

    def test_last_remaining_music_ignores_the_closing_block(self):
        snap = self.closer_hour()
        snap = replace(snap, queue=snap.queue[:3] + (replace(snap.queue[3], category_id=RECURRENT),)
                       + snap.queue[4:])
        last, reason = last_remaining_music(snap)
        self.assertEqual((last.log_item_id, last.category_id, reason), (4, RECURRENT, None))

    def test_last_remaining_music_is_only_from_the_active_log(self):
        # The previous hour's song still on the deck across the rollover is not
        # this hour's category source; only closers remain in the active log.
        carried = item(90, 300.0, category=RECURRENT, log=LOG - 1, played=True)
        closers = [item(4, 60.0, kind="spot", category=PSA), item(5, 10.0, kind="legal_id", category=LEGAL)]
        snap = snapshot(closers, 0, [deck(carried, 20.0)])
        self.assertEqual(last_remaining_music(snap), (None, "no_remaining_music"))

    def test_last_remaining_music_fails_closed_on_unknown_kind(self):
        snap = self.closer_hour()
        snap = replace(snap, queue=snap.queue[:5] + (replace(snap.queue[5], category_kind=None),))
        self.assertEqual(last_remaining_music(snap), (None, "remaining_kind_unknown"))


def candidate(track_id, effective, *, rank=0, keys=None, cue_in=0.0, category=GOLD):
    return Candidate(track_id, category, rank, cue_in, cue_in + effective, effective,
                     frozenset(keys or {f"artist{track_id}"}), f"/x/{track_id}.wav")


def frame(net):
    """A tail frame with a 0.0 s live-deck remainder and `net` of window."""
    from library.services.hour_refill_projection import TimingElement

    return FitFrame(window=net, prefix_fixed=0.0, last_prefix=TimingElement(1, 0.0, 100.0, position=100.0,
                                                                            role="deck"),
                    suffix_first_cue=None, suffix_total=0.0, allowance=0.0)


class BestFitSearchTests(SimpleTestCase):
    # Cases 9 and 10 -- deterministic, bounded one/two-track best fit.
    def test_one_track_fit(self):
        fit = search_best_fit([candidate(1, 150), candidate(2, 200), candidate(3, 260)], frame(205))
        self.assertEqual(([c.track_id for c in fit.items], fit.residual_seconds), ([2], 5.0))

    def test_two_track_fit_beats_a_worse_single(self):
        fit = search_best_fit([candidate(1, 150), candidate(2, 200), candidate(3, 250)], frame(400))
        self.assertEqual(sorted(c.track_id for c in fit.items), [1, 3])
        self.assertEqual(fit.residual_seconds, 0.0)

    def test_no_fit_never_overruns(self):
        fit = search_best_fit([candidate(1, 300), candidate(2, 301)], frame(299.999))
        self.assertEqual(fit.items, ())

    def test_exact_zero_residual_is_allowed_one_millisecond_over_is_not(self):
        self.assertEqual([c.track_id for c in search_best_fit([candidate(1, 200)], frame(200)).items], [1])
        self.assertEqual(search_best_fit([candidate(1, 200.001)], frame(200)).items, ())

    def test_original_source_wins_an_exact_tie(self):
        fit = search_best_fit([candidate(2, 180, rank=1), candidate(9, 180, rank=0)], frame(200))
        self.assertEqual([c.track_id for c in fit.items], [9])

    def test_identity_conflicting_pair_is_rejected(self):
        same_artist = [candidate(1, 100, keys={"x"}), candidate(2, 100, keys={"x"}), candidate(3, 90)]
        fit = search_best_fit(same_artist, frame(200))
        self.assertNotEqual(sorted(c.track_id for c in fit.items), [1, 2])
        self.assertEqual(fit.residual_seconds, 10.0)

    def test_deterministic_regardless_of_input_order(self):
        pool = [candidate(i, 90 + (i * 37) % 200, rank=i % 2) for i in range(1, 120)]
        expected = search_best_fit(pool, frame(333.3))
        for seed in range(5):
            shuffled = list(pool)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(search_best_fit(shuffled, frame(333.3)).items, expected.items)

    def test_count_budget_is_reported(self):
        pool = [candidate(i, 100 + i) for i in range(1, 60)]
        fit = search_best_fit(pool, frame(250), budget=100)
        self.assertTrue(fit.truncated)


class StalenessTests(SimpleTestCase):
    # Case 12 -- a new request or queue edit invalidates an advisory proposal.
    def proposal(self, snap):
        p = project_refill(snap, causes=[eject(1, 2)], boundary=boundary())
        return RefillProposal("proposed", (), projection=p, music=(ProposedItem(4242, GOLD, "original_cause", 90.0),),
                              expires_at=snap.captured_at + timedelta(seconds=PROPOSAL_TTL_SECONDS))

    def test_fresh_and_unchanged_is_not_stale(self):
        snap = hour([290, 290, 290])
        self.assertEqual(proposal_staleness(self.proposal(snap), fresh_snapshot=snap, now=snap.captured_at,
                                            active_request_track_ids=set()), ())

    def test_staleness_reasons(self):
        snap = hour([290, 290, 290])
        proposal = self.proposal(snap)
        reordered = replace(snap, queue=snap.queue[:3] + (snap.queue[4], snap.queue[3], snap.queue[5]))
        self.assertEqual(proposal_staleness(proposal, fresh_snapshot=reordered, now=snap.captured_at,
                                            active_request_track_ids=set()), ("queue_or_deck_changed",))
        self.assertEqual(proposal_staleness(proposal, fresh_snapshot=snap, now=snap.captured_at,
                                            active_request_track_ids={4242}), ("proposed_track_requested",))
        late = snap.captured_at + timedelta(seconds=PROPOSAL_TTL_SECONDS)
        self.assertEqual(proposal_staleness(proposal, fresh_snapshot=snap, now=late,
                                            active_request_track_ids=set()), ("expired",))
