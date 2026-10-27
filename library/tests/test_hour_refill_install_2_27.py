"""Roadmap 2.27 Phase C: guarded installer, ledger, coordinator.

Real rows and media files (Phase B fixtures), a bare PlaybackEngine stand-in
whose GLib side is simulated by GLibSpy: every idle callback runs under a
database blocker, so any query on the "GLib thread" fails the test. Races
are ordered with explicit hooks/barriers, never sleeps. install_permitted()
is patched where a test needs an install: production code has no bypass.

Set HOUR_REFILL_PHASE_C_EVIDENCE=<dir> to write representative results."""
import json
import os
import threading
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, TransactionTestCase

import library.services.engine as eng_module
from library.models import (
    HourRefillCause, HourRefillInstall, HourRefillPolicy, LogItem, PlaylistLog, Rotation, Track,
)
from library.services import hour_refill_install as inst
from library.services import hour_refill_projection as proj
from library.services import hour_refill_proposal as svc
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.tests.test_hour_refill_proposal_2_27 import DAY, GENERATED, RefillFixtures, _NoQueries, at, stand_in
from library.tests.test_schedule_minute_resolution_3_1c import make_row
from updatecenter.management.commands.updatecenter_probe import _serialize_exact_plan
from webrequests.models import SongRequest

REPO = Path(__file__).resolve().parents[2]
PERMIT = "library.services.hour_refill_install.install_permitted"


class GLibSpy:
    """Stands in for GLib.idle_add: runs callbacks at once (or later, when
    deferred) under a blocker that fails on ANY database query."""

    def __init__(self):
        self.calls, self.deferred, self.queued = 0, False, []

    def __call__(self, fn, *args):
        self.calls += 1
        if self.deferred:
            self.queued.append((fn, args))
            return 0
        with connection.execute_wrapper(_NoQueries()):
            fn(*args)
        return 0


class InstallFixtures(RefillFixtures):
    def engine(self, *, cursor, deck_index, deck_position, log=None, forced_indexes=()):
        log, items = self.queue(log)
        engine = stand_in(log, items, cursor=cursor, deck_index=deck_index, deck_position=deck_position,
                          forced=[items[i] for i in forced_indexes])
        engine._refill_coordinator = inst.RefillCoordinator()
        engine._last_queue_reload = 0.0
        engine.running = True
        return engine

    def installer(self, engine, *, now=None, hooks=None, psa_policy=None, clock=None):
        self.glib = GLibSpy()
        kwargs = dict(idle_add=self.glib, wall_clock=lambda: now or at(14, 40), hooks=hooks or {},
                      psa_policy=psa_policy)
        if clock is not None:
            kwargs["clock"] = clock
        return inst.HourRefillInstaller(engine, **kwargs)

    def eject_engine(self, future=(290.0, 290.0)):
        """Phase B's Eject hour: window 1200 s, runway 810.75 s, net 389.25 s."""
        self.eject_hour(future)
        return self.engine(cursor=3, deck_index=2, deck_position=10.0)

    def attempt(self, engine, **kwargs):
        with patch(PERMIT, return_value=True):
            return self.installer(engine, **kwargs).attempt()

    def reload(self, engine):
        """The unchanged r0119 GLib reload (its connection recycle is a no-op
        here, as in the existing engine tests)."""
        engine._last_queue_reload = 0.0
        with patch.object(eng_module, "_close_old_connections"):
            engine._reload_queue_if_changed()
        return [item.id for item in engine.log_items], engine._queue_cursor

    def rows(self, log=None):
        return list((log or self.log).items.order_by("position").values_list("id", "position", "origin"))

    def record(self, name, result):
        target = os.environ.get("HOUR_REFILL_PHASE_C_EVIDENCE")
        if target:
            Path(target).mkdir(parents=True, exist_ok=True)
            (Path(target) / f"{name}.json").write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True))


class CoreInstallTests(InstallFixtures, TestCase):
    def setUp(self):
        self.build()

    # Case 1 and 4 -- one Eject, tail append, adoption via the existing reload.
    def test_eject_install_tail_append_and_reload_adoption(self):
        engine = self.eject_engine()
        a, b = self.track(200.0), self.track(180.0)
        before = self.rows()
        result = self.attempt(engine)
        self.record("01-eject-installed-tail", result)
        self.assertEqual(result.status, inst.STATUS_INSTALLED, result.reasons)
        self.assertEqual(sorted(result.track_ids), sorted([a.id, b.id]))
        self.assertEqual(result.placement_mode, "tail_append")
        # 1200 - (810.75 + 380.5 + 7.0 budget) = 1.75 s beyond the 7 s budget
        self.assertEqual((result.safety_budget_seconds, result.residual_beyond_budget_seconds), (7.0, 1.75))
        after = self.rows()
        self.assertEqual(after[:len(before)], before)  # existing ids/positions/origins untouched
        new = after[len(before):]
        self.assertEqual([r[0] for r in new], list(result.installed_log_item_ids))
        self.assertEqual([r[1] for r in new], [before[-1][1] + 1, before[-1][1] + 2])
        self.assertEqual({r[2] for r in new}, {LogItem.ORIGIN_REFILL_INSTALLED})
        self.assertFalse(LogItem.objects.filter(id__in=result.installed_log_item_ids)
                         .exclude(playback_claimed_at=None, played_at=None).exists())
        ledger = HourRefillInstall.objects.get()
        self.assertEqual((ledger.install_key, ledger.cause_ids), (result.install_key, [HourRefillCause.objects.get().id]))
        # GLib was only ever asked for two primitive captures.
        self.assertEqual(self.glib.calls, 2)
        # Adoption: the unchanged r0119 reload brings the rows in, cursor after the deck.
        self.assertEqual(engine._refill_coordinator.pending_adoption[0], self.log.id)
        ids, cursor = self.reload(engine)
        self.assertEqual(ids, [r[0] for r in after])
        self.assertEqual(cursor, 3)
        self.assertTrue(engine._refill_coordinator.blocks_legacy(self.log.id, engine.log_items) is False)

    def test_repeat_attempts_never_install_twice(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0), self.track(170.0), self.track(160.0)
        self.assertEqual(self.attempt(engine).status, inst.STATUS_INSTALLED)
        # Before the reload the committed rows are not in memory: refuse.
        self.assertEqual(self.attempt(engine).status, inst.STATUS_ADOPTION_PENDING)
        self.reload(engine)
        again = self.attempt(engine)
        self.record("07-repeat-after-install", again)
        self.assertNotEqual(again.status, inst.STATUS_INSTALLED)  # fresh net is now small
        self.assertEqual(HourRefillInstall.objects.count(), 1)
        self.assertEqual(LogItem.objects.filter(origin=LogItem.ORIGIN_REFILL_INSTALLED).count(), 2)

    def test_ledger_key_blocks_a_second_install_of_the_same_cause_set(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        cause = HourRefillCause.objects.get()
        HourRefillInstall.objects.create(
            install_key=inst.install_key_for(self.log.id, [cause.id]), playlist_log_id_snapshot=self.log.id,
            log_date=DAY, log_hour=14, cause_ids=[cause.id], placement_mode="tail_append",
            net_before_seconds=1.0, residual_after_seconds=0.0, safety_budget_seconds=6.0,
            snapshot_digest="x" * 64,
        )
        before = self.rows()
        result = self.attempt(engine)
        self.record("08-duplicate-install", result)
        self.assertEqual((result.status, result.reasons), (inst.STATUS_DUPLICATE, ("install_key_exists",)))
        self.assertEqual(self.rows(), before)

    # Case 2 -- shortened request; request state untouched.
    def test_shortened_request_install_leaves_the_request_alone(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        self.row(self.track(240.0), 1, played=True)
        original = self.track(300.0)
        swapped = self.row(self.track(210.0), 2)
        self.row(self.track(290.0), 3)
        request = SongRequest.objects.create(external_request_id="c-req", track=swapped.track,
                                             submitted_at=at(14, 1), status="scheduled", log_item=swapped,
                                             requester_name="Private Person", dedication_message="Hidden")
        self.request_cause(swapped, original)
        engine = self.engine(cursor=2, deck_index=1, deck_position=10.0)
        self.track(240.0), self.track(215.0)
        result = self.attempt(engine)
        self.record("02-request-installed", result)
        self.assertEqual(result.status, inst.STATUS_INSTALLED, result.reasons)
        request.refresh_from_db()
        self.assertEqual((request.status, request.log_item_id, request.fulfilled_at), ("scheduled", swapped.id, None))
        self.assertEqual(set(LogItem.objects.filter(id__in=result.installed_log_item_ids)
                             .values_list("category_id", flat=True)), {self.gold.id})
        text = json.dumps(result.as_dict())
        for secret in ("Private Person", "Hidden", "c-req"):
            self.assertNotIn(secret, text)

    # Case 3 -- mixed causes: one install, strict 60 s gate.
    def test_mixed_causes_make_one_install(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        original = self.track(300.0)
        swapped = self.row(self.track(210.0), 3)
        self.row(self.track(240.0), 4)
        self.eject_cause(ejected, minute=20)
        self.request_cause(swapped, original)
        engine = self.engine(cursor=3, deck_index=2, deck_position=10.0)
        self.track(250.0), self.track(250.0)
        result = self.attempt(engine)
        self.assertEqual(result.status, inst.STATUS_INSTALLED, result.reasons)
        ledger = HourRefillInstall.objects.get()
        self.assertEqual(sorted(ledger.cause_ids), sorted(HourRefillCause.objects.values_list("id", flat=True)))
        self.assertEqual(self.attempt(engine).status, inst.STATUS_ADOPTION_PENDING)

    # Case 5 -- insert before the protected closing block (+ residual PSA).
    def closer_engine(self, psa_seconds=60.0):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        self.row(self.track(290.0, self.recur), 3)
        self.closer_psa = self.row(self.track(psa_seconds, self.psa), 4)
        self.closer_legal = self.row(self.track(10.0, self.legal), 5)
        self.eject_cause(ejected)
        return self.engine(cursor=3, deck_index=2, deck_position=10.0)

    def test_insert_before_keeps_the_closing_block_last_and_in_order(self):
        engine = self.closer_engine()
        self.track(300.0, self.recur), self.track(290.0, self.recur)
        before = self.rows()
        result = self.attempt(engine)
        self.record("03-protected-closer-insert-before", result)
        self.assertEqual((result.status, result.placement_mode, result.anchor_log_item_id),
                         (inst.STATUS_INSTALLED, "insert_before", self.closer_psa.id), result.reasons)
        after = self.rows()
        ids = [r[0] for r in after]
        self.assertEqual(ids[:4], [r[0] for r in before[:4]])
        self.assertEqual(ids[4:6], list(result.installed_log_item_ids))
        self.assertEqual(ids[6:], [self.closer_psa.id, self.closer_legal.id])
        self.assertEqual([r[1] for r in after], list(range(8)))
        self.assertEqual({r[0]: r[2] for r in after}[self.closer_legal.id], GENERATED)
        # Reload (and a restart's fresh load) gives exactly this order; cursor after the deck.
        self.assertEqual(self.reload(engine), (ids, 3))
        restarted = self.engine(cursor=3, deck_index=2, deck_position=12.0)
        self.assertEqual([item.id for item in restarted.log_items], ids)

    def test_insert_before_with_residual_psa(self):
        engine = self.closer_engine()
        music = self.track(300.0, self.recur)
        self.track(260.0, self.recur)
        psa = self.track(40.0, self.psa)
        result = self.attempt(engine, psa_policy=svc.PsaResidualPolicy(True, self.psa.id))
        self.assertEqual(result.status, inst.STATUS_INSTALLED, result.reasons)
        self.assertEqual(result.track_ids[-1], psa.id)
        self.assertIn(music.id, result.track_ids)
        ids = [r[0] for r in self.rows()]
        self.assertEqual(ids[-2:], [self.closer_psa.id, self.closer_legal.id])
        installed_psa = LogItem.objects.get(id=result.installed_log_item_ids[-1])
        self.assertEqual(installed_psa.category_id, self.psa.id)

    # Case 7 -- already satisfied (long deck, existing fill).
    def test_long_live_deck_installs_nothing(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(1620.0), 2, played=True)
        self.eject_cause(ejected)
        engine = self.engine(cursor=3, deck_index=2, deck_position=30.0)
        self.track(200.0)
        result = self.attempt(engine, now=at(14, 33, 30))
        self.record("05-long-deck-already-satisfied", result)
        self.assertEqual(result.status, proj.OUTCOME_ALREADY_SATISFIED)
        self.assertFalse(HourRefillInstall.objects.exists())

    def test_existing_live_fill_rows_already_cover_the_gap(self):
        self.eject_hour()
        fill = self.row(self.track(380.0), 5, origin=LogItem.ORIGIN_LIVE_FILL)
        engine = self.engine(cursor=3, deck_index=2, deck_position=10.0)
        self.track(200.0)
        result = self.attempt(engine)
        self.assertIn(result.status, (proj.OUTCOME_BELOW_THRESHOLD, proj.OUTCOME_ALREADY_SATISFIED))
        self.assertFalse(LogItem.objects.filter(origin=LogItem.ORIGIN_REFILL_INSTALLED).exists())
        self.assertIsNotNone(fill)

    # Case 8 -- an exact Phase B fit is not safe enough.
    def test_exact_zero_residual_is_rejected_but_a_budgeted_fit_installs(self):
        engine = self.eject_engine()
        self.track(389.0)   # Phase B: residual 0.0 -> no room for the 6 s single-item budget
        result = self.attempt(engine)
        self.assertEqual(result.status, svc.STATUS_NO_FIT)
        self.assertFalse(HourRefillInstall.objects.exists())
        marginal = self.track(383.0)   # 1200 - (810.75 + 383.25 + 6.0) = 0.0 beyond the budget
        result = self.attempt(engine)
        self.assertEqual((result.status, result.track_ids, result.residual_beyond_budget_seconds),
                         (inst.STATUS_INSTALLED, (marginal.id,), 0.0))

    def test_budget_is_positive_and_grows_per_item(self):
        frame = svc.FitFrame(window=100.0, prefix_fixed=0.0, last_prefix=proj.TimingElement(1, 0.0, 1.0, 1.0),
                             suffix_first_cue=None, suffix_total=0.0, allowance=0.0,
                             reserve_base=inst.SAFETY_BUDGET_BASE_SECONDS,
                             reserve_per_item=inst.SAFETY_BUDGET_PER_ITEM_SECONDS)
        self.assertEqual([frame.reserve(n) for n in (1, 2, 3)], [6.0, 7.0, 8.0])

    # Case 9 -- boundaries.
    def test_future_intra_hour_transition_installs_nothing(self):
        engine = self.eject_engine()
        profile = ensure_schedule_profile_state().active_profile
        make_row(profile, 14, 50, rotation=Rotation.objects.create(name="C-later"), dow=DAY.weekday())
        self.track(200.0)
        result = self.attempt(engine)
        self.assertEqual((result.status, result.reasons),
                         (proj.OUTCOME_NO_SAFE_BOUNDARY, ("future_intra_log_transition_unmodelled",)))
        self.assertFalse(HourRefillInstall.objects.exists())

    def test_partial_takeover_continuation_installs_within_the_takeover(self):
        log13 = PlaylistLog.objects.create(date=DAY, hour=13, status="approved")
        self.row(self.track(240.0), 0, log=log13, played=True, minute=40)
        ejected = self.row(self.track(240.0), 1, log=log13, played=True, minute=50)
        self.row(self.track(310.0), 2, log=log13, played=True, minute=58)
        self.row(self.track(290.0), 3, log=log13, minute=59)
        self.row(self.track(240.0), 0, minute=20)  # hour 14 takes over at 14:20
        self.eject_cause(ejected, minute=55)
        engine = self.engine(log=log13, cursor=3, deck_index=2, deck_position=10.0)
        self.track(200.0), self.track(100.0)
        result = self.attempt(engine, now=at(14, 5))
        self.record("04-partial-takeover-installed", result)
        self.assertEqual(result.status, inst.STATUS_INSTALLED, result.reasons)
        self.assertEqual(result.log_id, log13.id)
        # 900 - (590.5 + 300.5 + 7.0) = 2.0 beyond the budget: nothing crosses 14:20.
        self.assertEqual(result.residual_beyond_budget_seconds, 2.0)

    def test_insertion_too_close_to_its_start(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        self.eject_cause(ejected)
        engine = self.engine(cursor=3, deck_index=2, deck_position=225.0)  # 15.25 s to its trigger
        self.track(600.0)
        result = self.attempt(engine)
        self.assertEqual((result.status, result.reasons), (inst.STATUS_UNSAFE, ("insertion_too_soon",)))
        self.assertFalse(HourRefillInstall.objects.exists())

    # Case 17 -- dormant by default.
    def test_lead_is_measured_to_the_trigger_not_the_audible_start(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        self.eject_cause(ejected)
        engine = self.engine(cursor=3, deck_index=2, deck_position=212.0)  # 28.25 s to the audible start
        self.track(600.0, cue_in=10.0)  # ... but its trigger is 10 s earlier: 18.25 s
        result = self.attempt(engine)
        self.assertEqual((result.status, result.reasons), (inst.STATUS_UNSAFE, ("insertion_too_soon",)))

    def test_policy_off_or_dry_run_only_never_installs(self):
        engine = self.eject_engine()
        self.track(200.0)
        before = self.rows()
        from django.test.utils import CaptureQueriesContext

        for policy in (None, dict(enabled=False), dict(enabled=True)):
            HourRefillPolicy.objects.all().delete()
            if policy is not None:
                HourRefillPolicy.objects.create(**policy)  # dry_run_only stays True (check constraint)
            with CaptureQueriesContext(connection) as queries:
                result = self.installer(engine).attempt()
            self.assertEqual(result.status, inst.STATUS_DRY_RUN_ONLY)
            self.assertEqual(len(queries.captured_queries), 1)
        self.assertEqual(self.glib.calls, 0)
        self.assertEqual(self.rows(), before)
        self.assertFalse(HourRefillInstall.objects.exists())

    def test_engine_never_invokes_the_installer(self):
        source = (REPO / "library/services/engine.py").read_text()
        self.assertNotIn("HourRefillInstaller", source)
        self.assertNotIn(".attempt(", source)
        self.assertIn("RefillCoordinator()", source)

    # Case 19 -- dedication pair and existing rows untouched.
    def test_dedication_pair_and_existing_rows_untouched(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        intro = self.row(self.track(8.0, self.talk), 3, origin=LogItem.ORIGIN_ENGINE_INSERTED)
        song = self.row(self.track(200.0), 4)
        SongRequest.objects.create(external_request_id="c-ded", track=song.track, submitted_at=at(14, 2),
                                   status="scheduled", log_item=song, intro_log_item=intro)
        self.eject_cause(ejected)
        engine = self.engine(cursor=3, deck_index=2, deck_position=10.0)
        self.track(250.0), self.track(250.0)
        snapshot = list(LogItem.objects.order_by("id").values_list(
            "id", "position", "track_id", "category_id", "origin", "playback_claimed_at", "played_at"))
        result = self.attempt(engine)
        self.assertEqual((result.status, result.placement_mode), (inst.STATUS_INSTALLED, "tail_append"))
        survivors = list(LogItem.objects.exclude(id__in=result.installed_log_item_ids).order_by("id").values_list(
            "id", "position", "track_id", "category_id", "origin", "playback_claimed_at", "played_at"))
        self.assertEqual(survivors, snapshot)
        ids = [r[0] for r in self.rows()]
        self.assertEqual(ids.index(song.id), ids.index(intro.id) + 1)


class MigrationCompatibilityTests(TestCase):
    # Case 16
    def test_0095_is_additive_for_the_update_center(self):
        plan = _serialize_exact_plan(MigrationLoader(connection), ["library.0095_hour_refill_install"])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[0]["operations"]],
                         [("AlterField", "additive"), ("CreateModel", "additive")])

    def test_ledger_is_append_only_and_constrained(self):
        row = HourRefillInstall.objects.create(
            install_key="k", playlist_log_id_snapshot=1, log_date=DAY, log_hour=1, placement_mode="tail_append",
            net_before_seconds=1.0, residual_after_seconds=0.0, safety_budget_seconds=6.0, snapshot_digest="d",
        )
        with self.assertRaises(ValueError):
            row.save()
        from django.db import IntegrityError

        for bad in (dict(install_key="k"), dict(install_key="k2", safety_budget_seconds=0.0),
                    dict(install_key="k3", placement_mode="bogus")):
            values = dict(install_key="z", playlist_log_id_snapshot=1, log_date=DAY, log_hour=1,
                          placement_mode="tail_append", net_before_seconds=1.0, residual_after_seconds=0.0,
                          safety_budget_seconds=6.0, snapshot_digest="d")
            values.update(bad)
            with self.assertRaises(IntegrityError), transaction.atomic():
                HourRefillInstall.objects.create(**values)

    def test_refill_installed_origin_is_never_an_eject_cause(self):
        from library.services.hour_refill_causality import _occurrence_source
        from types import SimpleNamespace

        row = LogItem(origin=LogItem.ORIGIN_REFILL_INSTALLED)
        self.assertEqual(_occurrence_source(SimpleNamespace(continuation_reason=None), row), "refill_installed")


class StaleStateTests(InstallFixtures, TestCase):
    """Case 10, 11, 14 -- every change between planning and commit abstains
    with the log unchanged."""

    def setUp(self):
        self.build()

    def assert_nothing_installed(self, result, before):
        self.assertNotEqual(result.status, inst.STATUS_INSTALLED)
        self.assertEqual(self.rows(), before)
        self.assertFalse(HourRefillInstall.objects.exists())
        self.assertFalse(LogItem.objects.filter(origin=LogItem.ORIGIN_REFILL_INSTALLED).exists())

    def run_mutation(self, mutate, *, hook="before_second_snapshot", engine=None, expected=inst.STATUS_STALE,
                     reason=None):
        engine = engine or self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        result = self.attempt(engine, hooks={hook: lambda *a: mutate(engine)})
        self.assert_nothing_installed(result, before)
        self.assertEqual(result.status, expected, result.reasons)
        if reason:
            self.assertIn(reason, result.reasons)
        return result

    def test_same_count_reorder_between_snapshots(self):
        def mutate(engine):
            engine.log_items[3], engine.log_items[4] = engine.log_items[4], engine.log_items[3]
        result = self.run_mutation(mutate, reason="queue_or_deck_changed")
        self.record("06-stale-queue-discarded", result)

    def test_same_duration_track_substitution(self):
        def mutate(engine):
            swap = self.track(290.0)
            LogItem.objects.filter(pk=engine.log_items[4].pk).update(track=swap)
            engine.log_items[4] = LogItem.objects.select_related(
                "track", "track__artist", "track__category", "track__category__kind",
                "category", "category__kind").get(pk=engine.log_items[4].pk)
        self.run_mutation(mutate, reason="queue_or_deck_changed")

    def test_cursor_and_deck_generation_changes(self):
        self.run_mutation(lambda e: setattr(e, "_queue_cursor", 4), reason="queue_or_deck_changed")

    def test_deck_generation_change(self):
        self.run_mutation(lambda e: setattr(e.decks["A"], "generation", 41), reason="queue_or_deck_changed")

    def test_ownership_changes_block(self):
        for flag, value in (("manual_mode", True), ("remote_dj_session", object()), ("_next_triggered", True),
                            ("_start_next_active", object())):
            with self.subTest(flag=flag):
                LogItem.objects.all().delete()
                HourRefillCause.objects.all().delete()
                PlaylistLog.objects.exclude(pk=self.log.pk).delete()
                self.run_mutation(lambda e, f=flag, v=value: setattr(e, f, v),
                                  expected=proj.OUTCOME_BLOCKED_TRANSPORT)

    def test_engine_restart_changes_the_instance(self):
        self.run_mutation(lambda e: setattr(e, "_engine_session_id", "eng-restarted"),
                          reason="engine_instance_changed")

    def test_forced_dedication_insertion(self):
        self.run_mutation(lambda e: e._forced_next_items.append(e.log_items[4]), reason="queue_or_deck_changed")

    def test_media_disappears_before_commit(self):
        engine = self.eject_engine()
        a, _b = self.track(200.0), self.track(180.0)
        before = self.rows()
        result = self.attempt(engine, hooks={"before_second_snapshot": lambda: Path(a.filepath).unlink()})
        self.assert_nothing_installed(result, before)
        self.assertEqual((result.status, result.reasons), (inst.STATUS_STALE, ("media_missing",)))

    def test_db_side_engine_advance_is_caught_under_the_lock(self):
        def claim(engine, *_):
            LogItem.objects.filter(pk=engine.log_items[3].pk).update(playback_claimed_at=at(14, 41))
        self.run_mutation(claim, hook="before_transaction", reason="engine_advanced")

    def test_row_added_by_another_process(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        added = {}

        def add(*_):
            added["row"] = LogItem.objects.create(playlist_log=self.log, position=50, scheduled_time=at(14, 59),
                                                  track=self.track(100.0), category=self.gold)
        result = self.attempt(engine, hooks={"before_transaction": add})
        self.assertEqual((result.status, result.reasons), (inst.STATUS_STALE, ("rows_changed",)))
        self.assertEqual([r for r in self.rows() if r[0] != added["row"].id], before)
        self.assertFalse(HourRefillInstall.objects.exists())

    def test_track_edited_or_restricted_or_requested(self):
        cases = (
            (lambda ids: Track.objects.filter(id__in=ids).update(ready2air=False), "track_changed"),
            (lambda ids: Track.objects.filter(id__in=ids).update(blocked_slots=[DAY.weekday() * 24 + 14]),
             "track_restricted"),
            (lambda ids: SongRequest.objects.create(external_request_id="late", track_id=ids[0],
                                                    submitted_at=at(14, 40)), "track_requested"),
        )
        previous = []
        for change, reason in cases:
            with self.subTest(reason=reason):
                # The change ran inside the rolled-back transaction, so the
                # previous pool is eligible again: remove it.
                Track.objects.filter(id__in=previous).delete()
                LogItem.objects.all().delete()
                HourRefillCause.objects.all().delete()
                SongRequest.objects.all().delete()
                PlaylistLog.objects.exclude(pk=self.log.pk).delete()
                engine = self.eject_engine()
                pool = [self.track(200.0), self.track(180.0)]
                previous = [t.id for t in pool]
                before = self.rows()
                result = self.attempt(engine, hooks={"locked": lambda c=change, p=pool: c([t.id for t in p])})
                self.assert_nothing_installed(result, before)
                self.assertEqual((result.status, result.reasons), (inst.STATUS_STALE, (reason,)))

    def test_rollback_after_writes_leaves_nothing(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        before = self.rows()
        ticks = iter([0.0, 10.0])  # S2 received at 0, commit check at 10 s
        result = self.attempt(engine, clock=lambda: next(ticks))
        self.assertEqual((result.status, result.reasons), (inst.STATUS_STALE, ("snapshot_too_old_at_commit",)))
        self.assert_nothing_installed(result, before)

    # Case 11 -- insert-before anchor problems never fall back to tail.
    def closer_engine(self):
        self.next_hour()
        self.row(self.track(240.0), 0, played=True)
        ejected = self.row(self.track(240.0), 1, played=True)
        self.row(self.track(240.0), 2, played=True)
        self.row(self.track(290.0, self.recur), 3)
        self.anchor = self.row(self.track(60.0, self.psa), 4)
        self.legal_row = self.row(self.track(10.0, self.legal), 5)
        self.eject_cause(ejected)
        engine = self.engine(cursor=3, deck_index=2, deck_position=10.0)
        self.track(300.0, self.recur), self.track(290.0, self.recur)
        return engine

    def test_anchor_claimed_under_the_lock_is_refused_without_fallback(self):
        engine = self.closer_engine()
        before = self.rows()
        result = self.attempt(engine, hooks={"locked": lambda: LogItem.objects.filter(pk=self.anchor.pk).update(
            playback_claimed_at=at(14, 41))})
        self.assert_nothing_installed(result, before)
        self.assertEqual(result.status, inst.STATUS_STALE)  # the future-row claim check fires first

    def test_anchor_request_link_is_refused(self):
        engine = self.closer_engine()
        before = self.rows()
        result = self.attempt(engine, hooks={"locked": lambda: SongRequest.objects.create(
            external_request_id="anchor", track=self.anchor.track, submitted_at=at(14, 3),
            status="fulfilled", log_item=self.anchor)})
        self.assert_nothing_installed(result, before)
        self.assertEqual((result.status, result.reasons), (inst.STATUS_NO_SAFE_ANCHOR, ("closing_block_request_link",)))

    def test_anchor_next_up_is_refused(self):
        engine = self.closer_engine()
        before = self.rows()
        result = self.attempt(engine, hooks={"before_second_snapshot": lambda: setattr(engine, "_queue_cursor", 4)})
        self.assert_nothing_installed(result, before)


class LiveFillArbitrationTests(InstallFixtures, TestCase):
    # Case 13 -- ordinary live fill and causal refill, both orderings.
    def setUp(self):
        self.build()

    def test_legacy_in_flight_blocks_the_refill(self):
        engine = self.eject_engine()
        self.track(200.0)
        engine._live_fill_in_progress = True
        self.assertEqual(self.attempt(engine).status, inst.STATUS_LIVE_FILL_PENDING)
        engine._live_fill_in_progress = False
        engine._refill_coordinator.legacy_callback_pending = True  # worker done, callback not yet run
        self.assertEqual(self.attempt(engine).status, inst.STATUS_LIVE_FILL_PENDING)
        self.assertFalse(HourRefillInstall.objects.exists())

    def legacy_dispatch(self, engine):
        engine._last_live_extend_attempt = 0.0
        engine._live_fill_generation = 0
        with patch.object(eng_module.threading, "Thread") as thread, \
                connection.execute_wrapper(_NoQueries()), \
                patch.object(eng_module.timezone, "localtime", return_value=at(14, 40)):
            engine._try_extend_live_log_async()
        return thread.called

    def test_refill_in_flight_blocks_legacy_dispatch_on_glib_without_queries(self):
        engine = self.eject_engine()
        engine._refill_coordinator.refill_owner = object()
        self.assertFalse(self.legacy_dispatch(engine))
        engine._refill_coordinator.refill_owner = None
        self.assertTrue(self.legacy_dispatch(engine))  # default-Off parity: ordinary dispatch proceeds

    def test_committed_refill_blocks_legacy_until_reloaded(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        result = self.attempt(engine)
        self.assertEqual(result.status, inst.STATUS_INSTALLED)
        self.assertFalse(self.legacy_dispatch(engine))
        self.reload(engine)
        coordinator = engine._refill_coordinator
        self.assertFalse(coordinator.blocks_legacy(self.log.id, engine.log_items))
        self.assertIsNone(coordinator.pending_adoption)

    def test_late_legacy_callback_after_a_refill_commit_is_discarded(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        stale_count = len(engine.log_items)
        self.assertEqual(self.attempt(engine).status, inst.STATUS_INSTALLED)
        engine._refill_coordinator.legacy_callback_pending = True
        picks = [{"track": self.track(120.0), "category": self.gold}]
        with patch.object(eng_module, "append_fill_items") as append:
            self.assertFalse(engine._install_live_fill(self.log.id, 0, picks, stale_count, 99))
        append.assert_not_called()
        self.assertFalse(engine._refill_coordinator.legacy_callback_pending)
        result_late = engine._refill_coordinator.legacy_discards
        self.assertEqual(result_late, 1)

    def test_legacy_worker_marks_its_callback_pending(self):
        engine = self.eject_engine()
        engine._live_fill_in_progress = True
        hour_start = at(14)
        with patch.object(eng_module, "fill_remaining_hour",
                          return_value=([{"track": None}, {"track": self.track(100.0), "category": self.gold}], 0)), \
                patch.object(eng_module.GLib, "idle_add") as idle_add, \
                patch.object(eng_module, "_close_old_connections"), \
                patch.object(eng_module.connection, "close"):
            engine._live_fill_worker(self.log.id, 1, [{"track": None}], 1, 0.0, hour_start, 9, 300.0)
        idle_add.assert_called_once()
        self.assertTrue(engine._refill_coordinator.legacy_callback_pending)
        self.assertFalse(engine._live_fill_in_progress)
        self.assertEqual(self.attempt(engine).status, inst.STATUS_LIVE_FILL_PENDING)


class ConcurrentInstallTests(InstallFixtures, TransactionTestCase):
    """Case 12 and 14 -- real separate connections, ordered by barriers."""

    def setUp(self):
        ensure_schedule_profile_state()
        self.build()

    def test_two_engines_racing_the_same_log_install_once(self):
        # Two engines model two processes: each has its own coordinator.
        self.eject_hour()
        self.track(200.0), self.track(180.0)
        engines = [self.engine(cursor=3, deck_index=2, deck_position=10.0) for _ in range(2)]
        barrier = threading.Barrier(2, timeout=10)
        results = [None, None]

        def worker(i):
            try:
                results[i] = inst.HourRefillInstaller(
                    engines[i], idle_add=GLibSpy(), wall_clock=lambda: at(14, 40),
                    hooks={"before_transaction": lambda *a: barrier.wait()},
                ).attempt()
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        with patch(PERMIT, return_value=True):  # once, from this thread only
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        statuses = sorted(r.status for r in results)
        self.assertEqual(statuses.count(inst.STATUS_INSTALLED), 1, statuses)
        self.assertTrue(set(statuses) - {inst.STATUS_INSTALLED} <= {
            inst.STATUS_DUPLICATE, inst.STATUS_LOCK_CONTENDED, inst.STATUS_STALE})
        self.assertEqual(HourRefillInstall.objects.count(), 1)
        self.assertEqual(LogItem.objects.filter(origin=LogItem.ORIGIN_REFILL_INSTALLED).count(), 2)

    def test_same_engine_second_worker_is_refused_in_memory(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        inside, release = threading.Event(), threading.Event()
        results = {}

        def first():
            try:
                results["first"] = inst.HourRefillInstaller(
                    engine, idle_add=GLibSpy(), wall_clock=lambda: at(14, 40),
                    hooks={"before_transaction": lambda *a: (inside.set(), release.wait(10))},
                ).attempt()
            finally:
                connections.close_all()

        t = threading.Thread(target=first)
        with patch(PERMIT, return_value=True):  # once, from this thread only
            t.start()
            self.assertTrue(inside.wait(10))
            results["second"] = inst.HourRefillInstaller(engine, idle_add=GLibSpy(),
                                                         wall_clock=lambda: at(14, 40)).attempt()
            release.set()
            t.join(30)
        self.assertEqual(results["second"].status, inst.STATUS_REFILL_IN_FLIGHT)
        self.assertEqual(results["first"].status, inst.STATUS_INSTALLED)
        self.assertEqual(HourRefillInstall.objects.count(), 1)

    def test_run_in_worker_owns_its_own_connection(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        results = {}

        def worker():
            results["result"] = inst.HourRefillInstaller(engine, idle_add=GLibSpy(),
                                                         wall_clock=lambda: at(14, 40)).run_in_worker()
            results["closed"] = connection.connection is None

        t = threading.Thread(target=worker)
        with patch(PERMIT, return_value=True):
            t.start()
            t.join(30)
        self.assertEqual(results, {"result": results["result"], "closed": True})
        self.assertEqual(results["result"].status, inst.STATUS_INSTALLED)

    def test_lock_contention_abstains_with_no_writes(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        held, done = threading.Event(), threading.Event()

        def holder():
            try:
                with transaction.atomic():
                    PlaylistLog.objects.select_for_update().get(pk=self.log.pk)
                    held.set()
                    done.wait(10)
            finally:
                connections.close_all()

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(held.wait(10))
        before = self.rows()
        result = self.attempt(engine)
        done.set()
        t.join(30)
        self.assertEqual((result.status, result.reasons), (inst.STATUS_LOCK_CONTENDED, ("lock_timeout_or_deadlock",)))
        self.assertEqual(self.rows(), before)
        self.assertFalse(HourRefillInstall.objects.exists())


class CommitAdoptionTests(InstallFixtures, TestCase):
    # Case 15 -- DB success, GLib adoption delayed / never / engine moved / restart.
    def setUp(self):
        self.build()

    def test_adoption_never_runs_and_the_engine_restarts(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        result = self.attempt(engine)
        self.assertEqual(result.status, inst.STATUS_INSTALLED)
        # The old engine never reloads. A restart loads the log from the database.
        restarted = self.engine(cursor=3, deck_index=2, deck_position=40.0)
        self.assertEqual([i.id for i in restarted.log_items][-2:], list(result.installed_log_item_ids))
        again = self.attempt(restarted, now=at(14, 40, 30))
        self.assertNotEqual(again.status, inst.STATUS_INSTALLED)
        self.assertEqual(HourRefillInstall.objects.count(), 1)

    def test_engine_moves_to_another_log_before_adoption(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        result = self.attempt(engine)
        self.assertEqual(result.status, inst.STATUS_INSTALLED)
        record = self.record
        record("09-post-commit-engine-moved", result)
        nxt = PlaylistLog.objects.get(hour=15)
        engine.current_log = nxt
        ids, _cursor = self.reload(engine)
        self.assertTrue(set(result.installed_log_item_ids).isdisjoint(ids))  # never in the new queue
        self.assertFalse(LogItem.objects.filter(id__in=result.installed_log_item_ids)
                         .exclude(played_at=None).exists())  # never marked aired
        # Pending adoption is per log: legacy fill for the new log is not blocked.
        self.assertFalse(engine._refill_coordinator.blocks_legacy(nxt.id, engine.log_items))

    def test_engine_advances_normally_before_the_reload(self):
        engine = self.eject_engine()
        self.track(200.0), self.track(180.0)
        result = self.attempt(engine)
        # The deck item ends and the next queued item starts before the reload.
        next_item = engine.log_items[3]
        LogItem.objects.filter(pk=next_item.pk).update(playback_claimed_at=at(14, 44), played_at=at(14, 44))
        engine.decks["A"] = type(engine.decks["A"])("A", next_item, 5.0, generation=41)
        ids, cursor = self.reload(engine)
        self.assertEqual(ids[-2:], list(result.installed_log_item_ids))
        self.assertEqual(cursor, 4)  # just after the item now on the deck; new rows still ahead
