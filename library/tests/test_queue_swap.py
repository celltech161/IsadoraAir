"""Operator queue Swap: POST /api/engine/queue/replace/ (library.services.queue_replace) and the engine's
claim-time track identity check that closes the replacement/claim race (docs/QUEUE_SWAP.md)."""
import json
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from authz.models import Capability, GroupRole, Role, RoleCapability, ScheduleAccessConfig
from library import views
from library.models import Artist, Category, CategoryKind, GroupAccess, LogItem, PlaylistLog, Track
from library.services import queue_replace
from monitoring.models import SystemEvent
from webrequests.models import SongRequest

User = get_user_model()
INSTANCE = "engine-instance-1"


class SwapFixtures:
    def build(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        music, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        spot, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
        self.rock = Category.objects.create(code="SWAPROCK", name="Swap Rock", kind=music)
        self.oldies = Category.objects.create(code="SWAPOLD", name="Swap Oldies", kind=music)
        self.ids = Category.objects.create(code="SWAPID", name="Swap Legal ID", kind=spot)
        self.artist, _ = Artist.get_or_create_ci("Swap Test Artist")
        self.long = self.track("Long Song", self.rock, duration=300.0, next_start=295.0)
        self.short = self.track("Short Song", self.oldies, duration=120.0, next_start=116.0, cue_in=1.0)
        self.other = self.track("Other Song", self.rock, duration=200.0, next_start=196.0)
        self.legal_id = self.track("Legal ID", self.ids, duration=10.0, next_start=10.0)
        now = timezone.localtime()
        self.log = PlaylistLog.objects.create(date=now.date(), hour=now.hour, status="approved")
        self.items = [
            self.item(0, self.legal_id, self.ids, LogItem.ORIGIN_GENERATED_SCHEDULE),
            self.item(1, self.long, self.rock, LogItem.ORIGIN_GENERATED_SCHEDULE),
            self.item(2, self.other, self.rock, LogItem.ORIGIN_LIVE_FILL),
            self.item(3, self.other, self.rock, LogItem.ORIGIN_ENGINE_INSERTED),
        ]
        self.target = self.items[1]
        self.state = {
            "timestamp": time.time(), "state_revision": {"instance": INSTANCE, "seq": 9},
            "engine_session_id": INSTANCE, "log_id": self.log.id,
            "decks": {"A": {"log_item_id": self.items[0].id}, "B": None},
            "queue": [{"item_id": it.id, "track_id": it.track_id} for it in self.items[1:]],
        }

    def track(self, title, category, *, duration, next_start, cue_in=0.0, ready=True, with_file=True):
        path = Path(self._tmp.name) / f"{title.replace(' ', '_')}.mp3"
        if with_file:
            path.touch()
        return Track.objects.create(
            filepath=str(path), filename=path.name, title=title, artist=self.artist, category=category,
            ready2air=ready, duration_seconds=duration, next_start_seconds=next_start, cue_in_seconds=cue_in,
        )

    def item(self, position, track, category, origin):
        return LogItem.objects.create(
            playlist_log=self.log, position=position, scheduled_time=timezone.now() + timedelta(minutes=position),
            track=track, track_title=track.title, track_artist=self.artist.name, category=category, origin=origin,
        )

    def replace(self, item=None, *, expected=None, track=None, instance=INSTANCE, state="default"):
        item = item or self.target
        return queue_replace.replace_upcoming_item(
            item_id=item.id, expected_track_id=expected if expected is not None else item.track_id,
            track_id=(track or self.short).id, engine_instance=instance,
            state=self.state if state == "default" else state,
        )


class ReplaceServiceTests(SwapFixtures, TestCase):
    def setUp(self):
        self.build()

    def test_replaces_in_place_preserving_identity_position_category_and_origin(self):
        before = LogItem.objects.get(pk=self.target.pk)
        count = LogItem.objects.filter(playlist_log=self.log).count()
        outcome = self.replace()
        self.assertTrue(outcome.ok and outcome.changed)
        self.assertEqual(outcome.previous_track_id, self.long.id)
        after = LogItem.objects.get(pk=self.target.pk)
        self.assertEqual((after.track_id, after.track_title, after.track_artist),
                         (self.short.id, "Short Song", "Swap Test Artist"))
        self.assertEqual((after.position, after.scheduled_time, after.category_id, after.origin,
                          after.playback_claimed_at, after.played_at),
                         (before.position, before.scheduled_time, self.rock.id, LogItem.ORIGIN_GENERATED_SCHEDULE,
                          None, None))
        self.assertEqual(LogItem.objects.filter(playlist_log=self.log).count(), count)   # nothing inserted
        self.assertEqual(list(LogItem.objects.filter(playlist_log=self.log).order_by("position")
                              .values_list("id", flat=True)), [it.id for it in self.items])

    def test_repeating_a_committed_replacement_is_an_idempotent_no_op(self):
        self.assertTrue(self.replace().changed)
        again = self.replace(expected=self.long.id)
        self.assertTrue(again.ok)
        self.assertFalse(again.changed)
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.short.id)

    def test_stale_expected_track_is_refused(self):
        LogItem.objects.filter(pk=self.target.pk).update(track=self.other)
        outcome = self.replace(expected=self.long.id)
        self.assertEqual(outcome.reason, "item_changed")
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.other.id)

    def test_engine_state_must_be_fresh_from_the_same_instance_and_list_the_item(self):
        self.assertEqual(self.replace(state=None).reason, "engine_unavailable")
        self.assertEqual(self.replace(instance="another-engine").reason, "engine_changed")
        self.assertEqual(self.replace(item=self.items[0]).reason, "on_deck")
        self.state["queue"] = [q for q in self.state["queue"] if q["item_id"] != self.target.id]
        self.assertEqual(self.replace().reason, "not_upcoming")
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.long.id)

    def test_stale_state_file_is_unavailable(self):
        path = Path(self._tmp.name) / "engine_state.json"
        path.write_text(json.dumps({**self.state, "timestamp": time.time() - 60}))
        self.assertIsNone(queue_replace.read_engine_state(path))
        path.write_text(json.dumps(self.state))
        self.assertEqual(queue_replace.read_engine_state(path)["state_revision"]["instance"], INSTANCE)
        path.write_text("{not json")
        self.assertIsNone(queue_replace.read_engine_state(path))

    def test_claimed_played_and_unapproved_rows_are_refused(self):
        LogItem.objects.filter(pk=self.target.pk).update(playback_claimed_at=timezone.now())
        self.assertEqual(self.replace().reason, "claimed")
        LogItem.objects.filter(pk=self.target.pk).update(playback_claimed_at=None, played_at=timezone.now())
        self.assertEqual(self.replace().reason, "played")
        LogItem.objects.filter(pk=self.target.pk).update(played_at=None)
        PlaylistLog.objects.filter(pk=self.log.pk).update(status="draft")
        self.assertEqual(self.replace().reason, "log_not_live")
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.long.id)

    def test_protected_rows_are_refused(self):
        self.state["queue"].insert(0, {"item_id": self.items[0].id})
        self.state["decks"]["A"] = None
        self.assertEqual(self.replace(item=self.items[0]).reason, "not_replaceable")       # spot-kind slot
        self.assertEqual(self.replace(item=self.items[3]).reason, "not_replaceable")       # engine insertion
        LogItem.objects.filter(pk=self.items[2].pk).update(origin=LogItem.ORIGIN_DST_REPLAY)
        self.assertEqual(self.replace(item=self.items[2]).reason, "not_replaceable")       # DST replay
        LogItem.objects.filter(pk=self.items[2].pk).update(origin=None)
        self.assertEqual(self.replace(item=self.items[2]).reason, "not_replaceable")       # unknown origin

    def test_listener_request_reservations_are_protected(self):
        request = SongRequest.objects.create(
            external_request_id="swap-req-1", track=self.long, requester_name="Listener", status="scheduled",
            submitted_at=timezone.now(), log_item=self.target, scheduled_at=timezone.now(),
        )
        self.assertEqual(self.replace().reason, "request_reserved")
        request.refresh_from_db()
        self.assertEqual((request.status, request.log_item_id, request.fulfilled_at), ("scheduled", self.target.id, None))
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.long.id)
        # A request that is only waiting (not reserved on this row) does not block.
        SongRequest.objects.filter(pk=request.pk).update(status="pending", log_item=None)
        self.assertTrue(self.replace().changed)
        self.assertEqual(SongRequest.objects.get(pk=request.pk).status, "pending")

    def test_replacement_must_be_ready_music_with_media(self):
        not_ready = self.track("Not Ready", self.rock, duration=100, next_start=99, ready=False)
        no_file = self.track("No File", self.rock, duration=100, next_start=99, with_file=False)
        self.assertEqual(self.replace(track=not_ready).reason, "track_ineligible")
        self.assertEqual(self.replace(track=no_file).reason, "track_ineligible")
        self.assertEqual(self.replace(track=self.legal_id).reason, "track_ineligible")    # spot, not music
        missing = Track(pk=987654)
        self.assertEqual(self.replace(track=missing).reason, "track_not_found")
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.long.id)


@override_settings(SECURE_SSL_REDIRECT=False)
class ReplaceEndpointTests(SwapFixtures, TestCase):
    def setUp(self):
        self.build()
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        self.staff = User.objects.create_superuser("swap_staff", "swap@example.invalid", "pw")
        self.client.force_login(self.staff)
        path = Path(self._tmp.name) / "engine_state.json"
        path.write_text(json.dumps(self.state))
        patcher = patch.object(queue_replace, "ENGINE_STATE_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, body, client=None):
        return (client or self.client).post(reverse("library:api-engine-queue-replace"), json.dumps(body),
                                            content_type="application/json")

    def body(self, **changes):
        return {"item_id": self.target.id, "expected_track_id": self.long.id, "track_id": self.short.id,
                "engine_instance": INSTANCE, **changes}

    def test_success_reports_the_replacement_and_the_effective_airtime_change(self):
        response = self.post(self.body())
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual((data["ok"], data["changed"], data["item_id"], data["track_id"], data["previous_track_id"]),
                         (True, True, self.target.id, self.short.id, self.long.id))
        # Effective airtime (next_start - cue_in), not raw durations: (116 - 1) - 295.
        self.assertEqual(data["airtime_change_seconds"], -180.0)
        event = SystemEvent.objects.filter(title="Queue item swapped by operator").latest("id")
        self.assertEqual((event.detail["log_item_id"], event.detail["previous_track_id"], event.detail["username"]),
                         (self.target.id, self.long.id, "swap_staff"))

    def test_refusals_carry_reason_and_message_and_change_nothing(self):
        response = self.post(self.body(expected_track_id=self.other.id))
        self.assertEqual((response.status_code, response.json()["reason"]), (409, "item_changed"))
        self.assertTrue(response.json()["error"])
        response = self.post(self.body(track_id=self.legal_id.id))
        self.assertEqual((response.status_code, response.json()["reason"]), (422, "track_ineligible"))
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.long.id)

    def test_malformed_bodies_are_rejected(self):
        for body in ({}, self.body(item_id="1"), self.body(track_id=True), self.body(engine_instance=""),
                     self.body(expected_track_id=-3)):
            self.assertEqual(self.post(body).status_code, 400, body)
        bad = self.client.post(reverse("library:api-engine-queue-replace"), "[]", content_type="application/json")
        self.assertEqual(bad.status_code, 400)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse("library:api-engine-queue-replace")).status_code, 405)

    def test_csrf_is_enforced(self):
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.staff)
        self.assertEqual(self.post(self.body(), client=strict).status_code, 403)
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.long.id)

    def test_user_without_queue_management_is_refused(self):
        plain = User.objects.create_user("swap_plain", password="pw")
        group = Group.objects.create(name="swap readers")
        GroupAccess.objects.create(group=group, allowed_prefixes="/api/")
        plain.groups.add(group)
        self.client.force_login(plain)
        self.assertEqual(self.post(self.body()).status_code, 403)
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.long.id)

    def test_queue_manager_outside_their_schedule_window_is_refused(self):
        user = User.objects.create_user("swap_host", password="pw")
        group = Group.objects.create(name="swap hosts")
        GroupAccess.objects.create(group=group, allowed_prefixes="/api/")
        GroupRole.objects.create(group=group, role=Role.objects.get(name="Remote Host"))
        user.groups.add(group)
        self.client.force_login(user)
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.save()
        response = self.post(self.body())                      # no TalentAssignment: outside any window
        self.assertEqual(response.status_code, 403)
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.long.id)
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        self.assertEqual(self.post(self.body()).status_code, 200)   # the same holder, in compatibility mode

    def test_seeded_remote_dj_group_cannot_reach_the_endpoint_and_is_not_offered_swap(self):
        host = User.objects.create_user("swap_remote_dj", password="pw")
        host.groups.add(Group.objects.get_or_create(name="remote_dj")[0])
        self.client.force_login(host)
        self.assertEqual(self.post(self.body()).status_code, 403)   # GroupAccess: path not listed
        self.assertFalse(views._console_capabilities(host)["queue_swap"])
        self.assertTrue(views._console_capabilities(self.staff)["queue_swap"])


class ConsoleSwapPresentationTests(TestCase):
    TEMPLATE = Path(__file__).resolve().parents[1] / "templates/library/dashboard.html"

    def test_one_shared_search_feeds_insert_and_swap(self):
        source = self.TEMPLATE.read_text()
        self.assertEqual(source.count("/api/tracks/?q="), 1)
        self.assertEqual(source.count("&per_page=200&ready2air=true"), 1)
        for caller in ("async function searchForInsert(q) {\n  const items = await fetchQueueSearchResults(q);",
                       "const items = await fetchQueueSearchResults(q);\n  if (generation !== swapSearchGen"):
            self.assertIn(caller, source)

    def test_swap_request_carries_the_full_binding_and_is_single_flight(self):
        source = self.TEMPLATE.read_text()
        self.assertIn("body: JSON.stringify({ item_id: ctx.itemId, expected_track_id: ctx.trackId, track_id: choice.id,\n"
                      "                             engine_instance: ctx.engineInstance }),", source)
        confirm = source[source.index("async function confirmSwap()"):]
        confirm = confirm[: confirm.index("\n}\n")]
        self.assertIn("if (!ctx || !choice || swapSubmitting || swapStale || connectionStale) return;", confirm)
        self.assertIn("swapSubmitting = true;", confirm)
        self.assertIn("#consoleRoot.connection-stale .qt-swap-btn", source)


class _ClaimStandIn:
    """Just enough engine for _claim_playback_occurrence / _reread_changed_occurrence."""

    def __init__(self):
        import library.services.engine as eng
        self.engine = object.__new__(eng.PlaybackEngine)
        self.engine._poison_skip_identities = set()
        self.engine.log_items = []


class EngineClaimIdentityTests(SwapFixtures, TransactionTestCase):
    def setUp(self):
        self.build()
        self.engine = _ClaimStandIn().engine
        self.engine.log_items = list(LogItem.objects.filter(playlist_log=self.log).order_by("position"))

    def test_a_replacement_committed_after_the_queue_reload_is_never_claimed_with_the_stale_track(self):
        stale = self.engine.log_items[1]                       # in-memory copy, still the long track
        self.assertTrue(self.replace().changed)                # committed after the reload
        self.assertFalse(self.engine._claim_playback_occurrence(stale))
        self.assertEqual(self.engine._claim_track_changed_item_id, stale.id)
        self.assertIsNone(LogItem.objects.get(pk=stale.pk).playback_claimed_at)   # nothing claimed
        fresh = self.engine._reread_changed_occurrence(stale)
        self.assertEqual((fresh.id, fresh.track_id, fresh.track.title), (stale.id, self.short.id, "Short Song"))
        self.assertIs(self.engine.log_items[1], fresh)         # the queue now holds the committed row
        self.assertTrue(self.engine._claim_playback_occurrence(fresh))
        self.assertIsNotNone(LogItem.objects.get(pk=stale.pk).playback_claimed_at)

    def test_an_unchanged_occurrence_claims_exactly_as_before(self):
        item = self.engine.log_items[2]
        self.assertTrue(self.engine._claim_playback_occurrence(item))
        self.assertFalse(hasattr(self.engine, "_claim_track_changed_item_id"))
        self.assertIsNotNone(item.playback_claimed_at)

    def test_an_already_claimed_occurrence_is_not_reconsidered(self):
        item = self.engine.log_items[2]
        self.assertTrue(self.engine._claim_playback_occurrence(item))
        LogItem.objects.filter(pk=item.pk).update(track=self.short)   # only possible outside the endpoint
        self.assertTrue(self.engine._claim_playback_occurrence(item))  # deck re-creation of a claimed occurrence

    def test_a_changed_occurrence_that_became_unplayable_is_not_reopened(self):
        stale = self.engine.log_items[1]
        self.replace()
        Path(self.short.filepath).unlink()
        self.assertIsNone(self.engine._reread_changed_occurrence(stale))

    def test_start_next_track_opens_the_committed_replacement_in_the_same_slot(self):
        engine = self.engine
        stale = engine.log_items[1]
        self.replace()
        opened = []

        def create_deck(slot, log_item, *args, **kwargs):
            if not engine._claim_playback_occurrence(log_item):
                return None
            opened.append((slot, log_item.id, log_item.track_id))
            return object()

        engine.decks = {"A": None, "B": None}
        engine._deck_slot_available = lambda slot: True
        engine._free_slot = lambda: "B"
        engine._next_queue_item = lambda: (stale, True)        # forced: skips request scheduling
        engine._create_deck = create_deck
        engine._on_log_exhausted = lambda slot: None
        engine._next_triggered = True
        engine._start_next_track(slot="B")
        self.assertEqual(opened, [("B", stale.id, self.short.id)])
        self.assertFalse(engine._next_triggered)


@override_settings(SECURE_SSL_REDIRECT=False)
class ReplaceClaimRaceTests(SwapFixtures, TransactionTestCase):
    """The replacement and the engine's claim serialize on the LogItem row lock."""

    def setUp(self):
        self.build()

    def _hold_claim_lock(self, ready, release):
        try:
            from django.db import transaction
            with transaction.atomic():
                LogItem.objects.select_for_update().get(pk=self.target.pk)
                LogItem.objects.filter(pk=self.target.pk).update(playback_claimed_at=timezone.now())
                ready.set()
                release.wait(5)
        finally:
            connections.close_all()

    def test_a_claim_in_progress_wins_and_the_swap_is_refused(self):
        ready, release = threading.Event(), threading.Event()
        worker = threading.Thread(target=self._hold_claim_lock, args=(ready, release))
        worker.start()
        self.assertTrue(ready.wait(5))
        try:
            outcome = self.replace()                         # waits up to the 1 s lock timeout
            self.assertEqual(outcome.reason, "contended")
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(self.replace().reason, "claimed")   # after the claim commits
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.long.id)

    def test_a_swap_that_commits_first_is_what_the_engine_opens(self):
        engine = _ClaimStandIn().engine
        engine.log_items = list(LogItem.objects.filter(playlist_log=self.log).order_by("position"))
        stale = engine.log_items[1]
        self.assertTrue(self.replace().changed)
        self.assertFalse(engine._claim_playback_occurrence(stale))
        fresh = engine._reread_changed_occurrence(stale)
        self.assertTrue(engine._claim_playback_occurrence(fresh))
        self.assertEqual(LogItem.objects.get(pk=self.target.pk).track_id, self.short.id)
        connection.close()
