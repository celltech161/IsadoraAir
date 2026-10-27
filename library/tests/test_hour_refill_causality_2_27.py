"""Roadmap 2.27 Phase A: hour-refill causality contract, schema and dry-run
trace. Evidence only -- see docs/roadmap/2_27_PHASE_A_CAUSALITY_CONTRACT.md.

The request-swap hook itself is exercised against real locked transactions
in webrequests/tests/test_hour_refill_request_cause_2_27.py."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from library.models import HourRefillCause, HourRefillPolicy, LogFillConfig
from library.services import hour_refill_causality as causality
from library.services.hour_refill_causality import (
    CauseRecord,
    EjectObservation,
    build_causality_trace,
    build_request_substitution_evidence,
    classify_eject_observation,
    load_cause_records,
    remaining_effective_airtime,
    request_music_threshold_exceeded,
)

REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2027, 3, 1, 14, 5, tzinfo=dt_timezone.utc)
HOUR = (date(2027, 3, 1), 14)


def _evidence(**overrides):
    values = dict(
        occurred_at=T0, playlist_log_id=10, log_date=HOUR[0], log_hour=HOUR[1],
        log_item_id=100, log_item_position=3,
        scheduled_category_id=7, scheduled_category_code="GOLD", scheduled_category_kind_code="music",
        original_track_id=1, original_track_title="Long Song", original_track_artist="Band",
        original_effective_airtime_seconds=300.0, replacement_track_id=2,
        replacement_effective_airtime_seconds=210.0, song_request_id=55,
    )
    values.update(overrides)
    return build_request_substitution_evidence(**values)


def _record(n, *, shortening=60.0, at=None, log_item=None, category=(7, "GOLD"),
            cause_type="request_substitution", hour=HOUR, key=None):
    return CauseRecord(
        id=n, source_key=key or f"k{n}", cause_type=cause_type,
        occurred_at=at or T0 + timedelta(seconds=n), log_id=10,
        log_date=hour[0], log_hour=hour[1], log_item_id=log_item or 100 + n,
        log_item_position=n, scheduled_category_id=category[0],
        scheduled_category_code=category[1], scheduled_category_kind_code="music",
        original_track_id=n, original_effective_airtime_seconds=300.0,
        replacement_track_id=1000 + n, replacement_effective_airtime_seconds=300.0 - shortening,
        observed_shortening_seconds=shortening, song_request_id=n,
    )


class RequestSubstitutionEvidenceTests(SimpleTestCase):
    def test_shorter_different_track_is_a_cause_with_scheduled_category(self):
        evidence, reason = _evidence()
        self.assertIsNone(reason)
        self.assertEqual(evidence.observed_shortening_seconds, 90.0)
        self.assertEqual(evidence.scheduled_category_code, "GOLD")
        self.assertEqual(evidence.original_track_id, 1)
        self.assertEqual(evidence.replacement_track_id, 2)
        self.assertEqual(evidence.source_key, "request_substitution:li100:req55:t1-2")

    def test_non_causes(self):
        cases = {
            "same_track": dict(replacement_track_id=1),
            "not_shorter": dict(replacement_effective_airtime_seconds=300.0),
            "not_music_slot": dict(scheduled_category_kind_code="psa"),
            "missing_request": dict(song_request_id=None),
            "missing_track": dict(original_track_id=None),
        }
        for expected, overrides in cases.items():
            with self.subTest(expected):
                evidence, reason = _evidence(**overrides)
                self.assertIsNone(evidence)
                self.assertEqual(reason, expected)
        evidence, reason = _evidence(replacement_effective_airtime_seconds=301.0)
        self.assertEqual((evidence, reason), (None, "not_shorter"))

    def test_sub_millisecond_difference_is_not_shorter(self):
        evidence, reason = _evidence(replacement_effective_airtime_seconds=299.9996)
        self.assertEqual((evidence, reason), (None, "not_shorter"))

    def test_small_shortening_is_still_recorded_threshold_is_not_applied_at_capture(self):
        evidence, _ = _evidence(replacement_effective_airtime_seconds=290.0)
        self.assertEqual(evidence.observed_shortening_seconds, 10.0)


class ThresholdTests(SimpleTestCase):
    def test_request_music_threshold_is_strictly_greater_than_sixty(self):
        self.assertFalse(request_music_threshold_exceeded(None))
        self.assertFalse(request_music_threshold_exceeded(59.99))
        self.assertFalse(request_music_threshold_exceeded(60))
        self.assertFalse(request_music_threshold_exceeded(60.0))
        self.assertTrue(request_music_threshold_exceeded(60.01))
        self.assertTrue(request_music_threshold_exceeded(61))


class EjectClassificationTests(SimpleTestCase):
    """Contract fixtures for the deferred live Eject hook."""

    GOOD = EjectObservation(
        engine_instance="eng-1", slot="A", expected_log_item_id=200, deck_present=True,
        deck_log_item_id=200, deck_generation=17, completion_claimed=True,
        occurrence_source="scheduled", on_air=True, scheduled_category_kind_code="music",
        position_seconds=100.0, cue_in_seconds=2.0, next_start_seconds=250.0, duration_seconds=260.0,
    )

    def test_completed_eject_of_live_music_counts_only_remaining_airtime(self):
        envelope, reason = classify_eject_observation(self.GOOD)
        self.assertIsNone(reason)
        self.assertEqual(envelope.remaining_effective_airtime_seconds, 150.0)
        self.assertEqual(envelope.source_key, "operator_eject:eng-1:A:g17:li200")
        self.assertEqual(envelope.deck_generation, 17)
        self.assertEqual(envelope.eject_position_seconds, 100.0)

    def test_unbound_command_is_accepted_when_otherwise_eligible(self):
        envelope, _ = classify_eject_observation(replace(self.GOOD, expected_log_item_id=None))
        self.assertIsNotNone(envelope)

    def test_remaining_airtime_never_uses_full_file_length(self):
        self.assertEqual(remaining_effective_airtime(0.5, 2.0, 250.0, 260.0), 248.0)   # before cue-in
        self.assertEqual(remaining_effective_airtime(100.0, 2.0, None, 260.0), 160.0)  # no next_start
        self.assertEqual(remaining_effective_airtime(255.0, 2.0, 250.0, 260.0), 0.0)   # past trigger
        self.assertEqual(remaining_effective_airtime(10.0, 0.0, None, None), 0.0)

    def test_non_causes(self):
        cases = {
            "empty_slot": dict(deck_present=False),
            "stale_binding": dict(expected_log_item_id=199),
            "already_completing": dict(completion_claimed=False),
            "identity_unknown": dict(deck_generation=None),
            "manual_hold": dict(manual_mode=True),
            "remote_dj_owner": dict(remote_dj_owner=True),
            "mic_live": dict(mic_live=True),
            "start_next_transition": dict(start_next_active=True),
            "vt_transition": dict(vt_phase="outro_playing"),
            "overlap": dict(other_live_deck=True),
            "transition_pending": dict(transition_pending=True),
            "not_scheduled_occurrence": dict(occurrence_source="insert"),
            "not_music": dict(scheduled_category_kind_code="legal_id"),
            "not_on_air": dict(on_air=False),
            "paused": dict(paused=True),
            "position_unknown": dict(position_seconds=None),
            "no_remaining_airtime": dict(position_seconds=250.0),
        }
        for expected, overrides in cases.items():
            with self.subTest(expected):
                envelope, reason = classify_eject_observation(replace(self.GOOD, **overrides))
                self.assertIsNone(envelope)
                self.assertEqual(reason, expected)

    def test_unproven_occurrence_sources_are_not_causes(self):
        self.assertEqual(EjectObservation(engine_instance="e", slot="A", expected_log_item_id=None,
                                          deck_present=True).occurrence_source, "unknown")
        for source in ("unknown", "continuation", "generated_fill", "live_fill",
                       "operator_inserted", "engine_inserted"):
            with self.subTest(source):
                _, reason = classify_eject_observation(replace(self.GOOD, occurrence_source=source))
                self.assertEqual(reason, "not_scheduled_occurrence")

    def test_partially_aired_duration_is_remaining_not_played(self):
        envelope, _ = classify_eject_observation(replace(self.GOOD, position_seconds=240.0))
        self.assertEqual(envelope.remaining_effective_airtime_seconds, 10.0)


class CausalityTraceTests(SimpleTestCase):
    def test_multiple_causes_ordered_deterministically_and_summed_once(self):
        records = [_record(3, shortening=30.0), _record(1, shortening=90.0), _record(2, shortening=45.0)]
        trace = build_causality_trace(records, active_log_date=HOUR[0], active_log_hour=HOUR[1])
        self.assertEqual([r.id for r in trace.causes], [1, 2, 3])
        self.assertEqual(trace.observed_shortening_seconds, 165.0)
        self.assertIsNone(trace.net_deficit_seconds)
        self.assertFalse(trace.authoritative)
        # Same input in any order gives the same trace.
        again = build_causality_trace(list(reversed(records)), active_log_date=HOUR[0], active_log_hour=HOUR[1])
        self.assertEqual(again, trace)

    def test_same_instant_ties_break_on_id(self):
        records = [_record(2, at=T0), _record(1, at=T0)]
        trace = build_causality_trace(records, active_log_date=HOUR[0], active_log_hour=HOUR[1])
        self.assertEqual([r.id for r in trace.causes], [1, 2])

    def test_duplicate_source_key_and_duplicate_occurrence_are_not_double_counted(self):
        records = [
            _record(1, shortening=90.0, key="same"),
            _record(2, shortening=90.0, key="same"),
            _record(3, shortening=20.0, log_item=500),
            _record(4, shortening=20.0, log_item=500),
        ]
        trace = build_causality_trace(records, active_log_date=HOUR[0], active_log_hour=HOUR[1])
        self.assertEqual([r.id for r in trace.causes], [1, 3])
        self.assertEqual(trace.observed_shortening_seconds, 110.0)
        self.assertEqual(
            trace.duplicates,
            (("same", "duplicate_source_key"), ("k4", "duplicate_occurrence_cause")),
        )

    def test_request_and_eject_of_the_same_item_are_distinct_causes(self):
        records = [
            _record(1, shortening=90.0, log_item=500),
            _record(2, shortening=100.0, log_item=500, cause_type="operator_eject"),
        ]
        trace = build_causality_trace(records, active_log_date=HOUR[0], active_log_hour=HOUR[1])
        self.assertEqual(trace.observed_shortening_seconds, 190.0)

    def test_future_hour_cause_is_listed_but_not_counted(self):
        future = _record(2, shortening=120.0, hour=(HOUR[0], 15))
        trace = build_causality_trace([_record(1), future], active_log_date=HOUR[0], active_log_hour=HOUR[1])
        self.assertEqual([r.id for r in trace.causes], [1])
        self.assertEqual(trace.other_hour_causes, (future,))
        self.assertEqual(trace.observed_shortening_seconds, 60.0)

    def test_original_categories_kept_in_causal_order_with_origins(self):
        records = [
            _record(1, category=(9, "RECENT")),
            _record(2, category=(7, "GOLD")),
            _record(3, category=(9, "RECENT")),
        ]
        trace = build_causality_trace(records, active_log_date=HOUR[0], active_log_hour=HOUR[1])
        self.assertEqual(
            [(s.category_code, s.cause_keys) for s in trace.original_category_sources],
            [("RECENT", ("k1", "k3")), ("GOLD", ("k2",))],
        )

    def test_empty_trace(self):
        trace = build_causality_trace([], active_log_date=HOUR[0], active_log_hour=HOUR[1])
        self.assertEqual(trace.causes, ())
        self.assertEqual(trace.observed_shortening_seconds, 0.0)


def _cause_row(n=1, **overrides):
    values = dict(
        source_key=f"request_substitution:li{n}:req{n}:t1-2", cause_type="request_substitution",
        occurred_at=T0 + timedelta(seconds=n), playlist_log_id_snapshot=10,
        log_date=HOUR[0], log_hour=HOUR[1], log_item_id_snapshot=n, log_item_position=n,
        scheduled_category_id_snapshot=7, scheduled_category_code="GOLD",
        scheduled_category_kind_code="music", original_track_id_snapshot=1,
        original_track_title="Long Song", original_effective_airtime_seconds=300.0,
        replacement_track_id_snapshot=2, replacement_effective_airtime_seconds=210.0,
        observed_shortening_seconds=90.0, song_request_id_snapshot=n,
    )
    values.update(overrides)
    return HourRefillCause(**values)


class CauseSchemaTests(TestCase):
    def test_source_key_is_unique(self):
        _cause_row().save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            _cause_row(log_item_id_snapshot=99).save()

    def test_rows_are_append_only(self):
        row = _cause_row()
        row.save()
        row.observed_shortening_seconds = 1.0
        with self.assertRaises(ValueError):
            row.save()
        self.assertEqual(HourRefillCause.objects.get().observed_shortening_seconds, 90.0)

    def test_check_constraints(self):
        bad = {
            "zero shortening": dict(observed_shortening_seconds=0.0),
            "negative shortening": dict(observed_shortening_seconds=-5.0),
            "request without request id": dict(song_request_id_snapshot=None),
            "request without replacement": dict(replacement_track_id_snapshot=None),
            "eject without engine evidence": dict(cause_type="operator_eject"),
            "unknown type": dict(cause_type="start_next"),
            "non phase-a state": dict(state="installed"),
        }
        for label, overrides in bad.items():
            with self.subTest(label):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    _cause_row(**overrides).save()
        self.assertEqual(HourRefillCause.objects.count(), 0)

    def test_eject_row_with_engine_evidence_is_valid(self):
        _cause_row(
            source_key="operator_eject:eng-1:A:g17:li200", cause_type="operator_eject",
            song_request_id_snapshot=None, replacement_track_id_snapshot=None,
            replacement_effective_airtime_seconds=None, engine_instance="eng-1",
            deck_slot="A", deck_generation=17, eject_position_seconds=100.0,
            observed_shortening_seconds=150.0,
        ).save()
        self.assertEqual(HourRefillCause.objects.count(), 1)

    def test_load_cause_records_is_ordered_and_filtered(self):
        _cause_row(2).save()
        _cause_row(1).save()
        _cause_row(3, log_hour=15).save()
        records = load_cause_records(log_date=HOUR[0], log_hour=HOUR[1])
        self.assertEqual([r.log_item_id for r in records], [1, 2])
        self.assertEqual(records[0].scheduled_category_code, "GOLD")
        self.assertEqual(records[0].original_effective_airtime_seconds, 300.0)


class PolicyTests(TestCase):
    def test_missing_row_means_everything_off_and_is_not_created(self):
        self.assertEqual(HourRefillPolicy.capture_flags(), (False, False))
        self.assertFalse(HourRefillPolicy.objects.exists())

    def test_defaults_are_off_and_dry_run(self):
        policy = HourRefillPolicy()
        policy.save()
        policy.refresh_from_db()
        self.assertEqual(
            (policy.enabled, policy.capture_request_substitution, policy.capture_operator_eject, policy.dry_run_only),
            (False, False, False, True),
        )
        self.assertEqual(HourRefillPolicy.capture_flags(), (False, False))

    def test_master_switch_gates_both_triggers(self):
        HourRefillPolicy(enabled=False, capture_request_substitution=True, capture_operator_eject=True).save()
        self.assertEqual(HourRefillPolicy.capture_flags(), (False, False))
        HourRefillPolicy(enabled=True, capture_request_substitution=True, capture_operator_eject=False).save()
        self.assertEqual(HourRefillPolicy.capture_flags(), (True, False))
        HourRefillPolicy(enabled=True, capture_request_substitution=False, capture_operator_eject=True).save()
        self.assertEqual(HourRefillPolicy.capture_flags(), (False, True))

    def test_singleton(self):
        HourRefillPolicy().save()
        HourRefillPolicy(enabled=True).save()
        self.assertEqual(HourRefillPolicy.objects.count(), 1)

    def test_dry_run_only_cannot_be_cleared_in_phase_a(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            HourRefillPolicy(dry_run_only=False).save()

    def test_policy_is_separate_from_log_fill_config(self):
        HourRefillPolicy(enabled=True, capture_request_substitution=True).save()
        self.assertFalse(LogFillConfig.objects.exists())


class OffParityStructureTests(SimpleTestCase):
    """Phase A must leave the engine, live fill and normal fill as r0119."""

    def test_no_fill_code_references_the_refill_evidence(self):
        for relative in (
            "library/services/log_builder.py",
            "webrequests/management/commands/refresh_song_request_statuses.py",
        ):
            source = (REPO / relative).read_text()
            with self.subTest(relative):
                self.assertNotIn("hour_refill", source)
                self.assertNotIn("HourRefill", source)

    def test_engine_touches_only_the_evidence_snapshot_and_worker(self):
        import re

        source = (REPO / "library/services/engine.py").read_text()
        names = set(re.findall(r"\b\w*(?:hour_refill|HourRefill)\w*\b", source, flags=re.IGNORECASE))
        # Phase C adds only the memory-only RefillCoordinator import
        # (hour_refill_install); the engine never calls the installer.
        self.assertEqual(names, {"hour_refill_causality", "hour_refill_evidence",
                                 "HourRefillEvidenceRecorder", "_hour_refill_evidence",
                                 "hour_refill_install"})
        self.assertNotIn("HourRefillCause", source)
        self.assertNotIn("HourRefillPolicy", source)

    def test_only_the_request_swap_calls_the_capture(self):
        callers = []
        for path in REPO.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts or path.name == "hour_refill_causality.py":
                continue
            if ".venv" in path.parts or "node_modules" in path.parts:
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if "record_request_substitution_cause" in text or "persist_request_substitution_evidence" in text:
                callers.append(str(path.relative_to(REPO)))
        self.assertEqual(callers, ["webrequests/services.py"])

    def test_causality_module_has_no_install_or_selection_surface(self):
        source = (REPO / "library/services/hour_refill_causality.py").read_text()
        for forbidden in ("append_fill_items", "fill_remaining_hour", "pick_track", "LogItem.objects.create",
                          "GLib", "idle_add", "PlayEvent", "mark_song_requests_aired"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, source)
        self.assertTrue(callable(causality.classify_eject_observation))
