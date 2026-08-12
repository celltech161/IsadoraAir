"""1.1 spec second-pass correction, item 2 -- matched pairs are a
build-time planning optimization only, NEVER protected content once
persisted. A matched pair is the decision that produced a good initial
log; it carries no ongoing significance after that. Once persisted,
both members of a pair are ordinary, independently-addressable LogItem
rows: freely replaceable via website Song Request substitution,
operator/DJ queue replacement, manual reordering, skip/eject/force-next
-- with zero special-case pairing metadata, zero "pair lock," zero
"restore the original mate" logic, and zero protection extended to the
untouched partner merely because it was once paired with the item that
got replaced.

Confirmed structurally by inspection FIRST, before writing any of this:
neither LogItem nor SongRequest carries any pairing field, and no
migration ever added one -- find_matched_pair/_score_pair produce a
PairResult purely as an in-memory scoring/selection artifact that never
survives past _persist_log/append_fill_items. These tests exist to
PROVE the already-correct mechanics (both call sites -- website request
fulfillment and the operator's normal track-swap endpoint -- have zero
awareness of how a LogItem's track was originally chosen), not to fix a
bug. See PROJECT_NOTES.md / the 1.1 second-pass correction report."""
import random
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone as django_timezone

from library.models import (
    Artist, Category, CategoryKind, LogItem, PlaylistLog, Rotation, RotationSlot, Track,
)
from library.services.log_builder import _build_from_rotation
from webrequests.models import SongRequest, WebRequestConfig
from webrequests.services import SCHEDULING_CONTENDED, maybe_schedule_song_request
import webrequests.services as services_module


def make_category(code, kind_code="music"):
    kind, _ = CategoryKind.objects.get_or_create(code=kind_code, defaults={"name": "Music"})
    return Category.objects.create(code=code, name=code, kind=kind, artist_separation=0, title_separation=0)


class _PairFixtureMixin:
    """Builds a genuine landing-mode matched pair via the real
    _build_from_rotation() walk (not a hand-constructed pair -- this
    must go through the actual pairing machinery so these tests prove
    something about the real system), then hands off to a request-
    fulfillment or operator-replacement scenario."""

    def setUp(self):
        super().setUp()
        self.category = make_category("PAIRPROTECT")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._track_counter = 0

    def make_track(self, title, artist_name, duration=200, **overrides):
        self._track_counter += 1
        artist, _ = Artist.get_or_create_ci(artist_name)
        real_path = Path(self._tmpdir.name) / f"track-{self._track_counter}.mp3"
        real_path.touch()
        defaults = dict(
            title=title, artist=artist, category=self.category,
            ready2air=True, filepath=str(real_path),
            duration_seconds=duration, next_start_seconds=duration,
        )
        defaults.update(overrides)
        return Track.objects.create(**defaults)

    def build_pair(self, target_date, hour, target_duration_seconds=400, seed=42):
        """One excellent pair (200+200) among poor-fit decoys -- same
        shape as test_rotation_pair_integration.py's
        LandingPairConsumesTwoSlotsTests, reused here rather than
        hand-building LogItems, so this test exercises the real
        pairing decision, not just its aftermath."""
        track_a = self.make_track("Pair A", "Pair Artist A", duration=200)
        track_b = self.make_track("Pair B", "Pair Artist B", duration=200)
        self.make_track("Decoy Short", "Decoy Artist C", duration=20)
        self.make_track("Decoy Long", "Decoy Artist D", duration=900)

        rotation = Rotation.objects.create(name=f"Pair Protect Rotation {hour}")
        RotationSlot.objects.create(rotation=rotation, position=0, category=self.category)
        RotationSlot.objects.create(rotation=rotation, position=1, category=self.category)

        random.seed(seed)
        log, error = _build_from_rotation(target_date, hour, rotation, target_duration_seconds=target_duration_seconds)
        self.assertIsNone(error)

        items = list(LogItem.objects.filter(playlist_log=log).order_by("position"))
        self.assertEqual(len(items), 2, "setup must actually produce a matched pair, not a fallback")
        item_ids = {i.track_id for i in items}
        self.assertEqual(item_ids, {track_a.id, track_b.id}, "setup must actually produce a matched pair, not a fallback")
        item_a = next(i for i in items if i.track_id == track_a.id)
        item_b = next(i for i in items if i.track_id == track_b.id)
        return log, track_a, track_b, item_a, item_b


class MatchedPairReplaceableByWebRequestTests(_PairFixtureMixin, TransactionTestCase):
    """A valid, eligible pending web request may freely replace either
    member of an already-persisted matched pair via the NORMAL request-
    fulfillment path (maybe_schedule_song_request) -- unmodified,
    unaware of pairing, exactly as it treats any other LogItem."""

    def setUp(self):
        super().setUp()
        WebRequestConfig.objects.all().delete()
        self.cfg = WebRequestConfig.objects.create(
            enabled=True, open_slots=list(range(168)), max_fulfilled_per_hour=4,
            lookahead_warning_minutes=60, expire_after_hours=6,
        )
        self.state_path = Path(self._tmpdir.name) / "engine_state.json"
        patcher = patch.object(services_module, "ENGINE_STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_pending_request_replaces_pair_member_a_without_restoring_or_protecting_b(self):
        log, track_a, track_b, item_a, item_b = self.build_pair(date(2027, 5, 3), 9)

        # Request fulfillment only operates against an APPROVED log.
        log.status = "approved"
        log.save(update_fields=["status"])

        # A materially LONGER requested track than the pair member it
        # replaces -- fulfilling it will run this hour long. That
        # overrun is explicitly allowed here: clock-drift recovery
        # (item 4) is what absorbs it on the NEXT hour, not a reason to
        # block an otherwise-eligible request. Normal request
        # eligibility/fulfillment semantics are used completely
        # unmodified -- nothing here special-cases the fact that item_a
        # came from a matched pair.
        requested_track = self.make_track("Requested Song", "Requester Favorite Artist", duration=600)
        request = SongRequest.objects.create(
            external_request_id="req-pair-a", track=requested_track,
            requester_name="Listener", status="pending",
            submitted_at=django_timezone.now(),
        )

        result = maybe_schedule_song_request(item_a)

        self.assertIsNot(result, SCHEDULING_CONTENDED)
        self.assertEqual(result.track_id, requested_track.id)
        # The caller's original in-memory object is left untouched --
        # the pre-existing locking/re-fetch contract, unaffected by
        # pairing.
        self.assertEqual(item_a.track_id, track_a.id)

        # The actual DB row for slot A is genuinely swapped.
        item_a.refresh_from_db()
        self.assertEqual(item_a.track_id, requested_track.id)

        # No "restore the original mate" logic exists, or should exist:
        # track_a was simply replaced, not preserved or reinserted
        # anywhere else in this log.
        self.assertFalse(LogItem.objects.filter(playlist_log=log, track_id=track_a.id).exists())

        # B, the untouched pair partner, carries ZERO protection merely
        # for having been A's mate -- still exactly what it was, and
        # nothing about A's substitution touched, rejected, or altered
        # it.
        item_b.refresh_from_db()
        self.assertEqual(item_b.track_id, track_b.id)

        request.refresh_from_db()
        self.assertEqual(request.status, "scheduled")
        self.assertEqual(request.log_item_id, item_a.id)

    def test_valid_pending_request_replaces_pair_member_b_symmetrically(self):
        """Same proof, targeting the OTHER pair member -- pairing
        carries no positional favoritism either."""
        log, track_a, track_b, item_a, item_b = self.build_pair(date(2027, 5, 4), 10, seed=7)

        log.status = "approved"
        log.save(update_fields=["status"])

        requested_track = self.make_track("Requested Song B-side", "Requester Other Artist", duration=45)
        request = SongRequest.objects.create(
            external_request_id="req-pair-b", track=requested_track,
            requester_name="Another Listener", status="pending",
            submitted_at=django_timezone.now(),
        )

        result = maybe_schedule_song_request(item_b)

        self.assertIsNot(result, SCHEDULING_CONTENDED)
        self.assertEqual(result.track_id, requested_track.id)

        item_b.refresh_from_db()
        self.assertEqual(item_b.track_id, requested_track.id)
        self.assertFalse(LogItem.objects.filter(playlist_log=log, track_id=track_b.id).exists())

        item_a.refresh_from_db()
        self.assertEqual(item_a.track_id, track_a.id)


@override_settings(SECURE_SSL_REDIRECT=False)  # plain-HTTP Django test client would
# otherwise get a 301 on every request, same as test_dashboard_view.py's pattern
class MatchedPairReplaceableByOperatorSwapTests(_PairFixtureMixin, TestCase):
    """Operator/DJ replacement of either pair member via the normal
    admin/queue-replacement endpoint (api_log_item_swap, PATCH /api/log/
    item/<id>/swap/) must not be rejected because the item originated in
    a matched pair -- that endpoint has zero pairing awareness by
    construction (it only ever checks the target log's draft/approved
    status and the incoming track_id), so this proves the already-
    correct behavior end-to-end through the real view."""

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_superuser("pairswap", "pairswap@example.invalid", "password")
        self.client.force_login(self.staff)

    def test_operator_swap_replaces_pair_member_a_without_restoring_or_protecting_b(self):
        # api_log_item_swap only operates on a DRAFT log -- the normal
        # operator workflow of adjusting an hour before it's approved.
        log, track_a, track_b, item_a, item_b = self.build_pair(date(2027, 5, 5), 11, seed=99)
        self.assertEqual(log.status, "draft")

        replacement_track = self.make_track("Operator Swap-In", "Operator Artist", duration=300)

        response = self.client.patch(
            f"/api/log/item/{item_a.id}/swap/",
            data={"track_id": replacement_track.id},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        item_a.refresh_from_db()
        self.assertEqual(item_a.track_id, replacement_track.id)
        # Original A track is simply gone from this log -- no restore
        # logic reinserts it.
        self.assertFalse(LogItem.objects.filter(playlist_log=log, track_id=track_a.id).exists())

        # B, the untouched pair partner, is completely unaffected -- no
        # protection, no compensating change, nothing.
        item_b.refresh_from_db()
        self.assertEqual(item_b.track_id, track_b.id)

    def test_operator_swap_replaces_pair_member_b_symmetrically(self):
        log, track_a, track_b, item_a, item_b = self.build_pair(date(2027, 5, 6), 12, seed=123)

        replacement_track = self.make_track("Operator Swap-In B", "Operator Artist B", duration=50)

        response = self.client.patch(
            f"/api/log/item/{item_b.id}/swap/",
            data={"track_id": replacement_track.id},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        item_b.refresh_from_db()
        self.assertEqual(item_b.track_id, replacement_track.id)
        self.assertFalse(LogItem.objects.filter(playlist_log=log, track_id=track_b.id).exists())

        item_a.refresh_from_db()
        self.assertEqual(item_a.track_id, track_a.id)
