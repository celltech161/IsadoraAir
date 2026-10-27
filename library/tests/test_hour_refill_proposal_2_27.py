"""Roadmap 2.27 Phase B: database-backed dry-run proposals.

Real PlaylistLog/LogItem/Track rows, real media files in a temp directory, a
bare PlaybackEngine stand-in (no GStreamer) for the snapshot capture, and the
real Phase A cause rows. Every proposal is advisory: the tests assert that the
service issues no INSERT/UPDATE/DELETE/locking SQL and that the station's rows
are unchanged afterwards.

Set HOUR_REFILL_PHASE_B_EVIDENCE=<dir> to write the representative dry-run
proposals as JSON (the tests' own outputs, no requester data)."""
import json
import os
import tempfile
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import library.services.engine as eng_module
from library.models import (
    Artist, Category, CategoryKind, HourRefillCause, HourRefillPolicy, LogItem, PlaylistLog, RecencyConfig,
    Rotation, Track,
)
from library.services import hour_refill_projection as proj
from library.services import hour_refill_proposal as svc
from library.services.hour_refill_causality import (
    EjectCauseEnvelope,
    EjectCauseEvidence,
    build_request_substitution_evidence,
    operator_eject_source_key,
)
from library.services.log_builder import effective_airtime_seconds
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.tests.test_schedule_minute_resolution_3_1c import make_row
from webrequests.models import SongRequest

REPO = Path(__file__).resolve().parents[2]
DAY = date(2027, 6, 1)  # a Tuesday
QUEUE_RELATED = ("track", "track__artist", "track__category", "track__category__kind", "category", "category__kind")
GENERATED = LogItem.ORIGIN_GENERATED_SCHEDULE


def at(hour, minute=0, second=0):
    return timezone.make_aware(datetime.combine(DAY, time(hour, minute, second)), timezone.get_current_timezone())


class _NoQueries:
    def __call__(self, execute, sql, params, many, context):
        raise AssertionError(f"snapshot capture issued SQL: {sql}")


class FakeDeck:
    def __init__(self, slot, log_item, position, generation=40):
        self.slot, self.log_item, self.track = slot, log_item, log_item.track
        self.position, self.generation = position, generation
        self.paused = self.finished = self.retirement_started = False
        self.air_start_recorded = True
        self.continuation_reason = None


def stand_in(log, items, *, cursor, deck_index, deck_position, forced=()):
    engine = object.__new__(eng_module.PlaybackEngine)
    engine._lock = threading.RLock()
    engine.current_log = log
    engine.log_items = items
    engine._queue_cursor = cursor
    engine._forced_next_items = list(forced)
    engine.decks = {"A": FakeDeck("A", items[deck_index], deck_position), "B": None}
    engine._engine_session_id = "eng-b"
    engine.manual_mode = False
    engine.remote_dj_session = None
    engine._vt = {"phase": "idle"}
    engine._start_next_active = None
    engine._next_triggered = False
    engine._resume_item = None
    engine._live_fill_in_progress = False
    engine._poison_skip_identities = set()
    engine._any_mic_live = lambda: False
    engine._get_deck_position = lambda deck: deck.position
    return engine


class RefillFixtures:
    def build(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        kinds = {code: CategoryKind.objects.get_or_create(code=code, defaults={"name": code})[0]
                 for code in ("music", "spot", "talk", "legal_id")}
        self.gold = Category.objects.create(code="B_GOLD", name="Gold", kind=kinds["music"])
        self.recur = Category.objects.create(code="B_RECUR", name="Recurrent", kind=kinds["music"])
        self.psa = Category.objects.create(code="B_PSA", name="PSA", kind=kinds["spot"])
        self.legal = Category.objects.create(code="B_LEGAL", name="Legal", kind=kinds["legal_id"])
        self.talk = Category.objects.create(code="B_TALK", name="Talk", kind=kinds["talk"])
        self.n = 0
        self.log = PlaylistLog.objects.create(date=DAY, hour=14, status="approved")
        ensure_schedule_profile_state()

    def track(self, seconds, category=None, *, artist=None, ready=True, cue_in=0.0, next_start=None, file=True,
              related="", blocked=None):
        self.n += 1
        path = Path(self.tmp.name) / f"t{self.n}.wav"
        if file:
            path.touch()
        artist_row, _ = Artist.objects.get_or_create(name=artist or f"Artist {self.n}")
        end = seconds if next_start is None else next_start
        return Track.objects.create(
            filepath=str(path), filename=path.name, title=f"T{self.n}", artist=artist_row,
            category=category or self.gold, ready2air=ready, duration_seconds=max(seconds, end),
            next_start_seconds=end, cue_in_seconds=cue_in, related_artists=related, blocked_slots=blocked or [],
        )

    def row(self, track, position, *, log=None, category=None, played=False, minute=None, origin=GENERATED):
        log = log or self.log
        when = timezone.make_aware(datetime.combine(log.date, time(log.hour, minute if minute is not None
                                                                     else min(position * 4, 59))),
                                   timezone.get_current_timezone())
        return LogItem.objects.create(
            playlist_log=log, position=position, scheduled_time=when, track=track, track_title=track.title,
            category=category or track.category, origin=origin,
            played_at=when if played else None, playback_claimed_at=when if played else None,
        )

    def next_hour(self, minute=0, hour=15):
        log = PlaylistLog.objects.create(date=DAY, hour=hour, status="approved")
        self.row(self.track(240.0), 0, log=log, minute=minute)
        return log

    def queue(self, log=None):
        log = PlaylistLog.objects.get(pk=(log or self.log).pk)
        return log, list(log.items.select_related(*QUEUE_RELATED).order_by("position"))

    def snap(self, *, cursor, deck_index, deck_position, now=None, log=None, forced_indexes=()):
        log, items = self.queue(log)
        forced = [items[i] for i in forced_indexes]
        engine = stand_in(log, items, cursor=cursor, deck_index=deck_index, deck_position=deck_position,
                          forced=forced)
        return proj.capture_playback_window(engine, now=now or at(14, 40))

    def eject_cause(self, row, *, shortening=120.0, minute=30, generation=None):
        generation = generation or row.id
        envelope = EjectCauseEnvelope(
            source_key=operator_eject_source_key("eng-b", "A", generation, row.id), engine_instance="eng-b",
            slot="A", deck_generation=generation, log_item_id=row.id, eject_position_seconds=100.0,
            remaining_effective_airtime_seconds=shortening,
        )
        log = row.playlist_log
        occurred = timezone.make_aware(datetime.combine(log.date, time(log.hour, minute)),
                                       timezone.get_current_timezone())
        evidence = EjectCauseEvidence(
            envelope=envelope, occurred_at=occurred,
            playlist_log_id=log.id, log_date=log.date, log_hour=log.hour, log_item_position=row.position,
            scheduled_category_id=row.category_id, scheduled_category_code=row.category.code,
            scheduled_category_kind_code="music", original_track_id=row.track_id,
            original_track_title=row.track_title, original_track_artist="",
            original_effective_airtime_seconds=effective_airtime_seconds(row.track),
        )
        return HourRefillCause.objects.create(**evidence.cause_fields())

    def request_cause(self, row, original, *, request_id=9001, minute=31):
        evidence, reason = build_request_substitution_evidence(
            occurred_at=at(14, minute), playlist_log_id=row.playlist_log_id, log_date=DAY, log_hour=14,
            log_item_id=row.id, log_item_position=row.position, scheduled_category_id=row.category_id,
            scheduled_category_code=row.category.code, scheduled_category_kind_code="music",
            original_track_id=original.id, original_track_title=original.title, original_track_artist="",
            original_effective_airtime_seconds=effective_airtime_seconds(original),
            replacement_track_id=row.track_id,
            replacement_effective_airtime_seconds=effective_airtime_seconds(row.track),
            song_request_id=request_id,
        )
        self.assertIsNone(reason)
        return HourRefillCause.objects.create(**evidence.cause_fields())

    def eject_hour(self, future=(290.0, 290.0), *, future_category=None):
        """14:40, boundary 15:00 (approved next log): items 0 played, 1
        ejected, 2 on the deck at 10 s (240 s), then `future`."""
        self.next_hour()
        self.played = self.row(self.track(240.0), 0, played=True)
        self.ejected = self.row(self.track(240.0), 1, played=True)
        self.on_deck = self.row(self.track(240.0), 2, played=True)
        self.future = [self.row(self.track(s, future_category), 3 + i) for i, s in enumerate(future)]
        self.eject_cause(self.ejected)
        return self.snap(cursor=3, deck_index=2, deck_position=10.0)

    def evaluate(self, snap, **kwargs):
        kwargs.setdefault("offline_dry_run", True)
        return svc.evaluate_hour_refill(snap, **kwargs)

    def record(self, name, proposal):
        target = os.environ.get("HOUR_REFILL_PHASE_B_EVIDENCE")
        if target:
            Path(target).mkdir(parents=True, exist_ok=True)
            (Path(target) / f"{name}.json").write_text(json.dumps(proposal.as_dict(), indent=2, sort_keys=True))


class SnapshotCaptureTests(RefillFixtures, TestCase):
    def setUp(self):
        self.build()

    def test_capture_copies_queue_loaded_rows_with_zero_queries(self):
        self.next_hour()
        rows = [self.row(self.track(240.0), i, played=i < 3) for i in range(5)]
        log, items = self.queue()
        engine = stand_in(log, items, cursor=3, deck_index=2, deck_position=12.5)
        with connection.execute_wrapper(_NoQueries()):
            snap = proj.capture_playback_window(engine, now=at(14, 40))
        self.assertEqual(snap.capture_errors, ())
        self.assertEqual((snap.active_log_id, snap.active_log_hour, snap.cursor), (self.log.id, 14, 3))
        self.assertEqual([it.log_item_id for it in snap.queue], [r.id for r in rows])
        self.assertEqual(snap.queue[0].category_kind, "music")
        self.assertEqual(snap.queue[0].origin, GENERATED)
        self.assertTrue(snap.queue[0].played and snap.queue[0].claimed)
        self.assertEqual((snap.decks[0].item.log_item_id, snap.decks[0].position_seconds), (rows[2].id, 12.5))
        self.assertEqual(len(snap.digest()), 64)

    def test_unloaded_relations_are_reported_never_queried(self):
        self.next_hour()
        for i in range(4):
            self.row(self.track(240.0), i, played=i < 3)
        log = PlaylistLog.objects.get(pk=self.log.pk)
        items = list(log.items.order_by("position"))  # no select_related
        engine = stand_in(log, items, cursor=3, deck_index=2, deck_position=12.5)
        engine.decks["A"].track = None
        with connection.execute_wrapper(_NoQueries()):
            snap = proj.capture_playback_window(engine, now=at(14, 40))
        self.assertTrue(any("track_not_loaded" in e for e in snap.capture_errors))
        self.assertEqual(svc.evaluate_hour_refill(snap, offline_dry_run=True).status,
                         proj.OUTCOME_INVALID_SNAPSHOT)

    def test_engine_does_not_call_phase_b(self):
        source = (REPO / "library/services/engine.py").read_text()
        for name in ("hour_refill_projection", "hour_refill_proposal", "capture_playback_window",
                     "evaluate_hour_refill"):
            self.assertNotIn(name, source)


class BoundaryResolutionTests(RefillFixtures, TestCase):
    def setUp(self):
        self.build()

    def bare(self):
        return proj.PlaybackWindowSnapshot(
            captured_at=at(14, 40), engine_instance="", active_log_id=self.log.id, active_log_date=DAY,
            active_log_hour=14, cursor=0, queue=(), forced=(), decks=(), poison_skip=frozenset(),
        )

    def test_approved_ordinary_next_log(self):
        nxt = self.next_hour(0)
        b = svc.resolve_boundary(self.bare())
        self.assertEqual((b.kind, b.instant, b.source, b.next_log_id), (proj.BOUNDARY_ORDINARY, at(15), "approved_next_log", nxt.id))
        self.assertEqual(b.handoff, "natural")

    def test_approved_partial_next_log_is_its_first_persisted_item(self):
        self.next_hour(20)
        b = svc.resolve_boundary(self.bare())
        self.assertEqual((b.kind, b.instant), (proj.BOUNDARY_PARTIAL, at(15, 20)))

    def test_empty_approved_next_log_is_unproven(self):
        PlaylistLog.objects.create(date=DAY, hour=15, status="approved")
        b = svc.resolve_boundary(self.bare())
        self.assertEqual((b.kind, b.reason), (proj.BOUNDARY_UNKNOWN, "next_log_has_no_items"))

    def test_schedule_resolution_when_not_yet_built(self):
        profile = ensure_schedule_profile_state().active_profile
        make_row(profile, 15, 0, rotation=Rotation.objects.create(name="B-base"), dow=DAY.weekday())
        b = svc.resolve_boundary(self.bare())
        self.assertEqual((b.kind, b.instant, b.source), (proj.BOUNDARY_ORDINARY, at(15), "schedule_resolution"))

    def test_schedule_partial_hour(self):
        profile = ensure_schedule_profile_state().active_profile
        make_row(profile, 15, 30, rotation=Rotation.objects.create(name="B-later"), dow=DAY.weekday())
        b = svc.resolve_boundary(self.bare())
        self.assertEqual((b.kind, b.instant), (proj.BOUNDARY_PARTIAL, at(15, 30)))

    def test_future_intra_log_program_transition_forces_abstention(self):
        self.next_hour(0)
        profile = ensure_schedule_profile_state().active_profile
        make_row(profile, 14, 0, rotation=Rotation.objects.create(name="B-current"), dow=DAY.weekday())
        make_row(profile, 14, 50, rotation=Rotation.objects.create(name="B-protected"), dow=DAY.weekday())
        b = svc.resolve_boundary(self.bare())
        self.assertEqual(
            (b.kind, b.source, b.reason),
            (proj.BOUNDARY_UNKNOWN, "schedule_resolution", "future_intra_log_transition_unmodelled"),
        )

    def test_passed_intra_log_transition_does_not_hide_next_boundary(self):
        self.next_hour(0)
        profile = ensure_schedule_profile_state().active_profile
        make_row(profile, 14, 0, rotation=Rotation.objects.create(name="B-before"), dow=DAY.weekday())
        make_row(profile, 14, 30, rotation=Rotation.objects.create(name="B-current"), dow=DAY.weekday())
        b = svc.resolve_boundary(self.bare())
        self.assertEqual((b.kind, b.instant), (proj.BOUNDARY_ORDINARY, at(15)))

    def test_blank_next_hour_is_unproven(self):
        ensure_schedule_profile_state()
        b = svc.resolve_boundary(self.bare())
        self.assertEqual((b.kind, b.reason), (proj.BOUNDARY_UNKNOWN, "next_hour_unscheduled"))


class RepresentativeDryRunTests(RefillFixtures, TestCase):
    """Deliverable 5: one Eject, one shortened request, the 40 s overrun-
    offset net, the long live deck, a protected closer, a partial-hour
    handoff."""

    def setUp(self):
        self.build()

    def pool(self, *seconds, category=None):
        return [self.track(s, category) for s in seconds]

    def test_eject_proposal(self):
        snap = self.eject_hour()
        best_a, best_b, *_ = self.pool(200.0, 188.0, 150.0, 120.0)
        p = self.evaluate(snap)
        self.record("01-eject-proposed", p)
        self.assertEqual(p.status, svc.STATUS_PROPOSED)
        self.assertEqual(p.projection.window_seconds, 1200.0)
        self.assertEqual(p.projection.runway.total_seconds, 810.75)  # 230.25 + 2 x 290.25
        self.assertEqual(p.net_before_seconds, 389.25)
        self.assertEqual(sorted(m.track_id for m in p.music), sorted([best_a.id, best_b.id]))
        self.assertEqual(p.residual_after_seconds, 0.75)
        self.assertEqual((p.placement.mode, p.placement.after_log_item_id), ("tail_append", self.future[-1].id))
        self.assertEqual([s.provenance for s in p.category_sources],
                         [(f"operator_eject:eng-b:A:g{self.ejected.id}:li{self.ejected.id}",
                           f"last_music:li{self.future[-1].id}")])
        self.assertFalse(p.installed)
        self.assertLessEqual(p.expires_at, snap.captured_at + timedelta(seconds=svc.PROPOSAL_TTL_SECONDS))

    def request_hour(self, f5_seconds):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        on_deck = self.row(self.track(240.0), 1, played=True)
        original = self.track(300.0)
        swapped = self.row(self.track(210.0), 2)  # the request's Track now holds the slot
        self.row(self.track(f5_seconds), 3)
        self.request_cause(swapped, original)
        return self.snap(cursor=2, deck_index=1, deck_position=10.0)

    def test_shortened_request_proposal(self):
        snap = self.request_hour(290.0)
        self.pool(240.0, 229.0, 228.0, 200.0)
        p = self.evaluate(snap)
        self.record("02-request-proposed", p)
        self.assertEqual(p.projection.threshold_seconds, 60.0)
        self.assertEqual(p.net_before_seconds, 469.25)  # 1200 - (230.25 + 210.25 + 290.25)
        self.assertEqual(p.status, svc.STATUS_PROPOSED)
        # 240 + 229 would need 469.5 s with two poll-tick allowances: 0.25 s over.
        self.assertEqual(sorted(m.effective_seconds for m in p.music), [228.0, 240.0])
        self.assertEqual(p.residual_after_seconds, 0.75)

    def test_overrun_offset_request_is_40s_net_and_no_music(self):
        # Runway was 50 s past the boundary before the 300 -> 210 swap.
        snap = self.request_hour(719.25)
        self.pool(30.0, 35.0, 39.0)
        p = self.evaluate(snap, psa_policy=svc.PsaResidualPolicy(True, self.psa.id))
        self.record("03-request-40s-net-below-threshold", p)
        self.assertEqual(p.net_before_seconds, 40.0)
        self.assertEqual(p.projection.qualified_causes[0].observed_shortening_seconds, 90.0)
        self.assertEqual(p.status, proj.OUTCOME_BELOW_THRESHOLD)
        self.assertEqual((p.music, p.psa, p.psa_status), ((), None, "not_evaluated"))  # no PSA-only repair

    def test_long_live_deck_is_already_satisfied(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(1620.0), 2, played=True)  # Grateful Dead Hour part 2
        self.eject_cause(ejected)
        self.pool(200.0, 188.0)
        p = self.evaluate(self.snap(cursor=3, deck_index=2, deck_position=30.0, now=at(14, 33, 30)))
        self.record("04-long-live-deck-already-satisfied", p)
        self.assertEqual(p.projection.runway.deck_remaining_seconds, 1590.25)
        self.assertEqual(p.projection.window_seconds, 1590.0)
        self.assertEqual((p.status, p.net_before_seconds), (proj.OUTCOME_ALREADY_SATISFIED, 0.0))

    def test_protected_closer_gets_an_insert_before_anchor(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        last_music = self.row(self.track(290.0, self.recur), 3)
        psa = self.row(self.track(60.0, self.psa), 4)
        legal = self.row(self.track(10.0, self.legal), 5)
        self.eject_cause(ejected)
        a, b = self.pool(300.0, 299.0, category=self.recur)
        p = self.evaluate(self.snap(cursor=3, deck_index=2, deck_position=10.0))
        self.record("05-protected-closer-insert-before", p)
        self.assertEqual(p.status, svc.STATUS_PROPOSED)
        self.assertEqual((p.placement.mode, p.placement.anchor_log_item_id), ("insert_before", psa.id))
        self.assertEqual(sorted(m.track_id for m in p.music), sorted([a.id, b.id]))
        self.assertEqual(p.residual_after_seconds, 9.5)
        sources = {s.category_id: s for s in p.category_sources}
        self.assertEqual(sources[self.recur.id].provenance, (f"last_music:li{last_music.id}",))
        self.assertNotIn(self.psa.id, sources)
        self.assertNotIn(self.legal.id, sources)
        self.assertEqual(list(self.log.items.order_by("position").values_list("id", flat=True))[-2:],
                         [psa.id, legal.id])

    def test_partial_hour_handoff_uses_the_takeover_not_the_top_of_hour(self):
        log13 = PlaylistLog.objects.create(date=DAY, hour=13, status="approved")
        self.row(self.track(240.0), 0, log=log13, played=True, minute=40)
        ejected = self.row(self.track(240.0), 1, log=log13, played=True, minute=50)
        self.row(self.track(310.0), 2, log=log13, played=True, minute=58)
        self.row(self.track(290.0), 3, log=log13, minute=59)
        self.row(self.track(240.0), 0, minute=20)  # hour 14 is partial: takes over at 14:20
        self.eject_cause(ejected, minute=55)
        self.pool(200.0, 109.0)
        snap = self.snap(log=log13, cursor=3, deck_index=2, deck_position=10.0, now=at(14, 5))
        p = self.evaluate(snap)
        self.record("06-partial-handoff-continuation", p)
        self.assertEqual((p.projection.boundary.kind, p.projection.boundary.instant),
                         (proj.BOUNDARY_PARTIAL, at(14, 20)))
        self.assertEqual(p.projection.window_seconds, 900.0)  # not 55 minutes to 15:00
        self.assertEqual(p.net_before_seconds, 309.5)
        self.assertEqual(p.status, svc.STATUS_PROPOSED)
        self.assertEqual(p.residual_after_seconds, 0.0)
        self.assertEqual(p.as_dict()["projection"]["boundary"]["instant"], "2027-06-01T14:20:00-05:00")


class CandidateEligibilityTests(RefillFixtures, TestCase):
    # Case 10 -- every invalid candidate is excluded; selection is strict.
    def setUp(self):
        self.build()

    def test_invalid_candidates_are_never_proposed(self):
        snap = self.eject_hour()
        committed_artist = self.future[0].track.artist.name
        good_a, good_b = self.track(200.0), self.track(188.0)
        perfect = 388.75  # alone, a zero-residual fit
        missing_file = self.track(perfect, file=False)
        not_ready = self.track(perfect, ready=False)
        blocked = self.track(perfect, blocked=[DAY.weekday() * 24 + 14])
        recent = self.track(perfect)
        earlier = PlaylistLog.objects.create(date=DAY, hour=10, status="approved")
        self.row(recent, 0, log=earlier, played=True)
        requested = self.track(perfect)
        SongRequest.objects.create(external_request_id="b-req", track=requested, submitted_at=at(14, 1))
        related = self.track(perfect, related=committed_artist)
        same_artist = self.track(perfect, artist=committed_artist)
        nonpositive = self.track(400.0, cue_in=400.0, next_start=388.0)
        too_short = self.track(20.0)
        wrong_category = self.track(perfect, self.psa)
        p = self.evaluate(snap)
        self.assertEqual(p.status, svc.STATUS_PROPOSED)
        self.assertEqual(sorted(m.track_id for m in p.music), sorted([good_a.id, good_b.id]))
        self.assertEqual(p.selection["media_unavailable_track_ids"], [missing_file.id])
        reasons = p.selection["pool_by_reason"]
        for reason in ("artist_separation", "nonpositive_airtime", "too_short"):
            self.assertIn(reason, reasons)
        proposed = {m.track_id for m in p.music}
        for bad in (missing_file, not_ready, blocked, recent, requested, related, same_artist, nonpositive,
                    too_short, wrong_category):
            self.assertNotIn(bad.id, proposed)

    def test_exclusion_context_covers_committed_deck_forced_cause_request_and_next_hour(self):
        snap = self.eject_hour()
        requested = self.track(100.0)
        SongRequest.objects.create(external_request_id="b-req2", track=requested, submitted_at=at(14, 1))
        p = proj.project_refill(snap, causes=svc.load_relevant_causes(snap), boundary=svc.resolve_boundary(snap))
        ctx = svc.build_exclusion_context(snap, p, svc.load_request_context())
        next_track = LogItem.objects.get(playlist_log__hour=15).track_id
        expected = {r.track_id for r in [self.played, self.ejected, self.on_deck, *self.future]}
        self.assertTrue(expected | {requested.id, next_track} <= ctx.hard_track_ids)
        from library.services.related_artists import track_identity_keys

        self.assertTrue(track_identity_keys(self.future[0].track.artist.name, "") <= ctx.hard_identity_keys)

    def test_no_fit_when_every_candidate_overruns(self):
        snap = self.eject_hour()
        self.track(389.1)  # shorter than the 389.25 s gap, but its own poll tick makes it 0.1 s over
        self.track(389.3)
        p = self.evaluate(snap)
        self.assertEqual(p.status, svc.STATUS_NO_FIT)
        self.assertEqual((p.selection["candidates"], p.selection["pool_by_reason"]["longer_than_gap"]), (1, 1))

    def test_recency_is_never_loosened_even_for_an_otherwise_empty_pool(self):
        # pick_track would halve separation and finally drop it; Phase B never does.
        snap = self.eject_hour()
        recent = self.track(389.0)
        earlier = PlaylistLog.objects.create(date=DAY, hour=12, status="approved")
        self.row(recent, 0, log=earlier, played=True)
        p = self.evaluate(snap)
        self.assertEqual((p.status, p.selection["candidates"]), (svc.STATUS_NO_FIT, 0))

    def test_one_track_fit(self):
        snap = self.eject_hour()
        only = self.track(389.0)
        p = self.evaluate(snap)
        self.assertEqual(([m.track_id for m in p.music], p.residual_after_seconds), ([only.id], 0.0))


class CategorySourceTests(RefillFixtures, TestCase):
    # Case 9 -- the two allowed sources, ranked together.
    def setUp(self):
        self.build()

    def test_best_fit_across_both_sources(self):
        snap = self.eject_hour(future_category=self.recur)
        self.track(150.0, self.gold)
        a, b = self.track(200.0, self.recur), self.track(188.0, self.recur)
        p = self.evaluate(snap)
        self.assertEqual(sorted(m.track_id for m in p.music), sorted([a.id, b.id]))
        self.assertEqual({m.category_source for m in p.music}, {svc.SOURCE_LAST_MUSIC})

    def test_original_cause_category_wins_an_exact_tie(self):
        snap = self.eject_hour(future_category=self.recur)
        gold_200, recur_200 = self.track(200.0, self.gold), self.track(200.0, self.recur)
        partner = self.track(188.0, self.gold)
        p = self.evaluate(snap)
        self.assertEqual(sorted(m.track_id for m in p.music), sorted([gold_200.id, partner.id]))
        self.assertNotIn(recur_200.id, [m.track_id for m in p.music])

    def test_multiple_original_categories_keep_causal_order(self):
        self.eject_hour(future_category=self.gold)
        recur_row = LogItem.objects.get(pk=self.played.pk)
        LogItem.objects.filter(pk=recur_row.pk).update(category=self.recur)
        self.eject_cause(LogItem.objects.get(pk=recur_row.pk), minute=10)
        self.track(389.0, self.recur)
        p = self.evaluate(self.snap(cursor=3, deck_index=2, deck_position=10.0))
        self.assertEqual([(s.category_id, s.rank) for s in p.category_sources],
                         [(self.recur.id, 0), (self.gold.id, 0)])
        self.assertEqual(p.music[0].category_source, svc.SOURCE_ORIGINAL)

    def test_no_eligible_music_category(self):
        doomed = Category.objects.create(code="B_GONE", name="Gone", kind=self.gold.kind)
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0, doomed), 1, played=True)
        self.row(self.track(600.0, self.talk), 2, played=True)
        self.row(self.track(60.0, self.psa), 3)
        self.eject_cause(ejected)
        doomed.delete()
        self.track(300.0, self.gold)
        p = self.evaluate(self.snap(cursor=3, deck_index=2, deck_position=10.0))
        self.assertEqual(p.status, svc.STATUS_NO_CATEGORY)
        self.assertEqual([s.status for s in p.category_sources], ["category_missing"])
        self.assertIn("no_remaining_music", p.reasons)


class ResidualPsaTests(RefillFixtures, TestCase):
    # Case 11 -- one optional PSA for 0 < residual <= 60, only when configured.
    def setUp(self):
        self.build()

    def run_with(self, music_seconds, policy, psa_seconds=59.75, psa_file=True):
        snap = self.eject_hour()
        self.track(music_seconds)
        psa = self.track(psa_seconds, self.psa, file=psa_file)
        return self.evaluate(snap, psa_policy=policy), psa

    def enabled(self):
        return svc.PsaResidualPolicy(True, self.psa.id)

    def test_residual_exactly_60_takes_one_psa(self):
        p, psa = self.run_with(329.0, self.enabled())
        self.assertEqual(p.selection and p.psa_status, "psa_proposed")
        self.assertEqual(p.psa.track_id, psa.id)
        self.assertEqual(p.residual_after_seconds, 0.0)

    def test_residual_above_60_is_not_a_psa_opportunity(self):
        p, _psa = self.run_with(328.99, self.enabled())
        self.assertEqual((p.psa, p.psa_status, p.residual_after_seconds),
                         (None, "not_applicable_residual_above_60s", 60.01))

    def test_zero_residual_needs_no_psa(self):
        p, _psa = self.run_with(389.0, self.enabled())
        self.assertEqual((p.psa, p.psa_status), (None, "not_applicable_no_residual"))

    def assert_psa_status(self, policy, status, **kwargs):
        p, _psa = self.run_with(329.0, policy, **kwargs)
        self.assertEqual((p.status, p.residual_after_seconds), (svc.STATUS_PROPOSED, 60.0))
        self.assertEqual((p.psa, p.psa_status), (None, status))

    def test_psa_not_enabled(self):
        self.assert_psa_status(None, "psa_disabled")

    def test_psa_disabled(self):
        self.assert_psa_status(svc.PsaResidualPolicy(False, self.psa.id), "psa_disabled")

    def test_psa_not_configured(self):
        self.assert_psa_status(svc.PsaResidualPolicy(True, None), "psa_not_configured")

    def test_psa_category_missing(self):
        self.assert_psa_status(svc.PsaResidualPolicy(True, 987654), "psa_category_missing")

    def test_psa_category_must_not_be_music(self):
        self.assert_psa_status(svc.PsaResidualPolicy(True, self.gold.id), "psa_category_not_psa_kind")

    def test_psa_file_unavailable(self):
        self.assert_psa_status(self.enabled(), "no_psa_fit", psa_file=False)

    def test_psa_too_long_for_the_residual(self):
        self.assert_psa_status(self.enabled(), "no_psa_fit", psa_seconds=59.76)


class NoSideEffectTests(RefillFixtures, TestCase):
    # Cases 12 and 13 -- zero writes, default-Off parity, stale detection.
    def setUp(self):
        self.build()

    def counts(self):
        return {model.__name__: model.objects.count()
                for model in (LogItem, PlaylistLog, Track, HourRefillCause, HourRefillPolicy, RecencyConfig,
                              SongRequest, Category)}

    def test_proposal_service_issues_no_writes_or_locks(self):
        snap = self.eject_hour()
        self.track(329.0)
        self.track(59.75, self.psa)
        before = self.counts()
        with CaptureQueriesContext(connection) as queries:
            p = self.evaluate(snap, psa_policy=svc.PsaResidualPolicy(True, self.psa.id))
        self.assertEqual((p.status, p.psa_status), (svc.STATUS_PROPOSED, "psa_proposed"))
        statements = [q["sql"].lstrip().upper() for q in queries.captured_queries]
        self.assertTrue(statements)
        for sql in statements:
            self.assertTrue(sql.startswith("SELECT"), sql)
            self.assertNotIn("FOR UPDATE", sql)
            self.assertNotIn("FOR SHARE", sql)
        self.assertEqual(self.counts(), before)
        json.dumps(p.as_dict())  # serialisable

    def test_feature_off_is_one_read_and_nothing_else(self):
        snap = self.eject_hour()
        HourRefillPolicy.objects.all().delete()
        with CaptureQueriesContext(connection) as queries:
            p = svc.evaluate_hour_refill(snap)
        self.assertEqual((p.status, len(queries.captured_queries)), (proj.OUTCOME_OFF, 1))
        HourRefillPolicy.objects.create(enabled=False)
        self.assertEqual(svc.evaluate_hour_refill(snap).status, proj.OUTCOME_OFF)

    def test_offline_dry_run_and_enabled_policy_never_change_the_policy(self):
        snap = self.eject_hour()
        self.track(389.0)
        HourRefillPolicy.objects.all().delete()
        self.assertEqual(svc.evaluate_hour_refill(snap, offline_dry_run=True).status, svc.STATUS_PROPOSED)
        self.assertFalse(HourRefillPolicy.objects.exists())
        HourRefillPolicy.objects.create(enabled=True)
        self.assertEqual(svc.evaluate_hour_refill(snap).status, svc.STATUS_PROPOSED)
        self.assertEqual(list(HourRefillPolicy.objects.values_list("enabled", "dry_run_only")), [(True, True)])

    def test_serialised_proposal_carries_no_requester_data(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        on_deck = self.row(self.track(240.0), 1, played=True)
        original = self.track(300.0)
        swapped = self.row(self.track(210.0), 2)
        self.row(self.track(290.0), 3)
        SongRequest.objects.create(external_request_id="secret-req", track=swapped.track, submitted_at=at(14, 1),
                                   requester_name="Private Person", dedication_message="Secret words",
                                   status="scheduled", log_item=swapped)
        self.request_cause(swapped, original)
        self.track(240.0)
        self.track(229.0)
        snap = self.snap(cursor=2, deck_index=1, deck_position=10.0)
        p = self.evaluate(snap)
        text = json.dumps(p.as_dict())
        self.assertEqual(p.status, svc.STATUS_PROPOSED)
        for secret in ("Private Person", "Secret words", "secret-req", "requester", "dedication_message"):
            self.assertNotIn(secret, text)
        self.assertIsNotNone(on_deck)
        self.assertEqual(p.as_dict()["projection"]["engine_instance"], snap.engine_instance)

    def test_new_request_or_same_count_edit_makes_the_proposal_stale(self):
        snap = self.eject_hour()
        chosen = self.track(389.0)
        p = self.evaluate(snap)
        self.assertEqual(svc.proposal_staleness(p, fresh_snapshot=snap, now=snap.captured_at,
                                                active_request_track_ids=svc.load_request_context().active_track_ids),
                         ())
        SongRequest.objects.create(external_request_id="late", track=chosen, submitted_at=at(14, 40))
        self.assertIn("proposed_track_requested", svc.proposal_staleness(
            p, fresh_snapshot=snap, now=snap.captured_at,
            active_request_track_ids=svc.load_request_context().active_track_ids))
        LogItem.objects.filter(pk=self.future[1].pk).update(track=self.track(290.0))  # same count
        fresh = self.snap(cursor=3, deck_index=2, deck_position=40.0)
        self.assertIn("queue_or_deck_changed", svc.proposal_staleness(
            p, fresh_snapshot=fresh, now=snap.captured_at, active_request_track_ids=set()))
