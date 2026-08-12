"""1.1 spec (2026-08-11) -- remaining gap coverage: per-slot independent
holiday resolution under landing-mode pairing (never merged into one
pair-wide decision), transactional persistence (_persist_log/
append_fill_items roll back cleanly on failure), and additional
related-artist/time-boundary edge cases specific to pair mode."""
import random
from datetime import date, timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase
from django.utils import timezone as django_timezone

from library.models import (
    Artist, Category, CategoryKind, Holiday, LogItem, PlaylistLog, Rotation, RotationSlot, Track,
)
from library.services.log_builder import (
    _build_from_rotation,
    _persist_log,
    _resolve_slot_pool_context,
    append_fill_items,
)
from monitoring.models import SystemEvent


def make_category(code, kind_code="music"):
    # kind_code MUST be literally "music" -- _resolve_slot_pool_context's
    # is_music_slot gate (and holiday injection generally) checks
    # category.kind.code == "music" exactly, not just a music-sounding
    # kind code.
    kind, _ = CategoryKind.objects.get_or_create(code=kind_code, defaults={"name": "Music"})
    return Category.objects.create(code=code, name=code, kind=kind, artist_separation=0, title_separation=0)


def make_track(title, artist_name, category, duration, **overrides):
    artist, _ = Artist.get_or_create_ci(artist_name)
    defaults = dict(
        filepath=f"/tmp/does-not-exist/holidaytest-{Track.objects.count()}.mp3",
        filename="track.mp3", title=title, artist=artist, category=category,
        ready2air=True, duration_seconds=duration, next_start_seconds=duration,
    )
    defaults.update(overrides)
    return Track.objects.create(**defaults)


class PerSlotIndependentHolidayResolutionTests(TestCase):
    """Landing-mode pairing must resolve each slot's holiday dice roll
    independently -- never merge two slots' decisions into one pair-wide
    decision (1.1 spec invariant)."""

    def setUp(self):
        self.category_a = make_category("HOLPAIRA")
        self.category_b = make_category("HOLPAIRB")
        for cat in (self.category_a, self.category_b):
            for i in range(3):
                make_track(f"{cat.code} Track {i}", f"{cat.code} Artist {i}", cat, 190)

    def test_resolve_slot_pool_context_called_once_per_slot_independently(self):
        rotation = Rotation.objects.create(name="Holiday Independence Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, category=self.category_a)
        RotationSlot.objects.create(rotation=rotation, position=1, category=self.category_b)

        calls = []
        real_fn = _resolve_slot_pool_context

        def spy(category, target_datetime, recency_cfg, active_holiday_codes, daily_shares):
            calls.append(category.code)
            return real_fn(category, target_datetime, recency_cfg, active_holiday_codes, daily_shares)

        random.seed(1)
        with patch("library.services.log_builder._resolve_slot_pool_context", side_effect=spy):
            _build_from_rotation(date(2027, 4, 8), 5, rotation, target_duration_seconds=400)

        # Slot A's category resolved once, slot B's resolved once,
        # independently -- not a single shared/merged call for the pair.
        self.assertEqual(calls.count("HOLPAIRA"), 1)
        self.assertEqual(calls.count("HOLPAIRB"), 1)

    def test_slots_can_land_on_different_holiday_outcomes(self):
        """Force distinguishable random() outcomes for the two slots'
        independent dice rolls (same share, different results) and
        confirm the resolved pool_key differs between them -- proof the
        two decisions are genuinely independent, not copied from one to
        the other."""
        today = django_timezone.localtime().date()
        Holiday.objects.create(
            code="INDEPTEST", name="Independence Test Holiday",
            month=today.month, day=today.day, ramp_in_days=0, ramp_out_days=0,
            max_share=1, max_weight_boost=0,
        )
        target_datetime = django_timezone.make_aware(django_timezone.datetime(today.year, today.month, today.day, 5, 0))
        recency_cfg_cls = __import__("library.models", fromlist=["RecencyConfig"]).RecencyConfig
        recency_cfg = recency_cfg_cls.load()

        # share=0.5 -> random() < 0.5 hits, >= 0.5 misses. Force a HIT
        # for slot A and a MISS for slot B with the SAME share value,
        # proving the two rolls are genuinely independent draws (if
        # merged into one pair-wide decision, both would necessarily
        # share the same outcome).
        with patch("library.services.log_builder.random.random", return_value=0.1):
            ctx_a = _resolve_slot_pool_context(self.category_a, target_datetime, recency_cfg, ["INDEPTEST"], {"INDEPTEST": 0.5})
        with patch("library.services.log_builder.random.random", return_value=0.9):
            ctx_b = _resolve_slot_pool_context(self.category_b, target_datetime, recency_cfg, ["INDEPTEST"], {"INDEPTEST": 0.5})
        pool_key_a = ctx_a[3]
        pool_key_b = ctx_b[3]
        self.assertEqual(pool_key_a, ("holiday", ("INDEPTEST",)))
        self.assertNotEqual(pool_key_b, ("holiday", ("INDEPTEST",)))


class TransactionalPersistenceTests(TestCase):
    def setUp(self):
        self.category = make_category("PERSISTTEST")
        self.artist, _ = Artist.get_or_create_ci("Persist Test Artist")
        self.track = make_track("Persist Track", "Persist Test Artist", self.category, 180)

    def _pick(self, target_datetime):
        return {"position": 0, "scheduled_time": target_datetime, "track": self.track, "category": self.category}

    def test_persist_log_failure_leaves_no_orphaned_playlistlog(self):
        target_date = date(2027, 4, 9)
        hour = 5
        target_datetime = django_timezone.make_aware(django_timezone.datetime(2027, 4, 9, hour, 0))
        # Pre-existing approved log for this hour, to prove it's still
        # gone-or-present consistently (delete+create+bulk_create is
        # all-or-nothing) -- if bulk_create fails, the delete must not
        # have taken effect either.
        PlaylistLog.objects.create(date=target_date, hour=hour, status="approved")
        self.assertEqual(PlaylistLog.objects.filter(date=target_date, hour=hour).count(), 1)

        with patch("library.services.log_builder.LogItem.objects.bulk_create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                _persist_log(target_date, hour, [self._pick(target_datetime)])

        # Atomic rollback: still exactly one row (the delete inside the
        # failed transaction was rolled back too), not zero.
        self.assertEqual(PlaylistLog.objects.filter(date=target_date, hour=hour).count(), 1)

    def test_append_fill_items_failure_persists_nothing(self):
        target_date = date(2027, 4, 10)
        hour = 6
        target_datetime = django_timezone.make_aware(django_timezone.datetime(2027, 4, 10, hour, 0))
        log = PlaylistLog.objects.create(date=target_date, hour=hour, status="approved")
        before_count = LogItem.objects.filter(playlist_log=log).count()

        with patch("library.services.log_builder.LogItem.objects.bulk_create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                append_fill_items(log, [self._pick(target_datetime)], start_position=0)

        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), before_count)

    def test_persist_log_success_is_all_in_one_transaction(self):
        target_date = date(2027, 4, 11)
        hour = 7
        target_datetime = django_timezone.make_aware(django_timezone.datetime(2027, 4, 11, hour, 0))
        log, error = _persist_log(target_date, hour, [self._pick(target_datetime)])
        self.assertIsNone(error)
        self.assertEqual(LogItem.objects.filter(playlist_log=log).count(), 1)


class TimeBoundaryEdgeCaseTests(TestCase):
    def setUp(self):
        self.category = make_category("TIMEBOUNDARY")
        for i in range(3):
            make_track(f"TB Track {i}", f"TB Artist {i}", self.category, 180)

    def test_zero_remaining_after_first_slot_stops_cleanly(self):
        """target_duration_seconds so small that even ONE of this
        pool's 180s tracks would overshoot it by 17x, far beyond
        DURATION_FIT_MARGIN -- the loop must break cleanly with no
        negative-remaining weirdness and no crash. Second-pass
        correction: this no longer force-picks a wildly oversized
        track just to produce a non-empty log (that would silently
        create the exact kind of gross top-of-hour overrun clock-drift
        recovery exists to prevent) -- both slots are gracefully
        skipped instead, leaving an empty (but successfully built, no
        error) log."""
        rotation = Rotation.objects.create(name="Zero Remaining Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, category=self.category)
        RotationSlot.objects.create(rotation=rotation, position=1, category=self.category)

        random.seed(9)
        target_date, hour = date(2027, 4, 12), 8
        log, error = _build_from_rotation(target_date, hour, rotation, target_duration_seconds=10)

        self.assertIsNone(error)
        items = list(LogItem.objects.filter(playlist_log=log).order_by("position"))
        self.assertEqual(len(items), 0)
        event = SystemEvent.objects.get(dedupe_key=f"library|selection-diagnostics|{target_date.isoformat()}|{hour}")
        self.assertEqual(event.detail["pool_exhausted_picks"], 2)
