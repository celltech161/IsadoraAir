"""Roadmap 3.1A -- schedule profile foundation.

Covers the model/constraint invariants, profile-scoped resolution, log
provenance, r0095-compatible /api/schedule/ behavior for a station with only
its migrated Default Schedule, the admin surface, and the real three-stage
migration (run through MigrationExecutor against the test database).
"""
import json
import tempfile
import uuid
from datetime import date, time as dt_time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from authz.models import Capability, GroupRole, Role, RoleCapability
from library.models import (
    Artist, Category, CategoryKind, GroupAccess, LogItem, Playlist, PlaylistItem,
    PlaylistLog, Rotation, ScheduleBlock, ScheduleProfile, ScheduleProfileState, ScheduleProfileStateError,
    Track,
)
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.services import log_builder
from library.services.log_builder import (
    _build_from_playlist,
    build_and_approve_hour_log_locked,
    build_hour_log,
    build_hour_log_for_admin,
    get_active_schedule_profile,
    preview_hour_log,
    resolve_schedule_block,
)

User = get_user_model()

MONDAY = date(2027, 3, 1)   # weekday() == 0
TUESDAY = date(2027, 3, 2)


def active_profile():
    return ScheduleProfileState.load().active_profile


def make_profile(name):
    return ScheduleProfile.objects.create(name=name)


class ScheduleFixtureMixin:
    def build_fixtures(self):
        # Recreate the migration's initial profile/state if an earlier
        # TransactionTestCase flush removed them (production never does).
        ensure_schedule_profile_state()
        self.rotation_a = Rotation.objects.create(name="Rotation A")
        self.rotation_b = Rotation.objects.create(name="Rotation B")
        self.playlist = Playlist.objects.create(name="Playlist P")

    def weekly(self, profile, hour, *, dow=0, rotation=None, playlist=None):
        return ScheduleBlock.objects.create(
            profile=profile, day_of_week=dow, start_time=dt_time(hour, 0),
            end_time=dt_time((hour + 1) % 24, 0),
            rotation=rotation if (rotation or playlist) else self.rotation_a, playlist=playlist,
        )

    def dated(self, profile, hour, target_date, *, rotation=None, playlist=None):
        return ScheduleBlock.objects.create(
            profile=profile, specific_date=target_date, start_time=dt_time(hour, 0),
            end_time=dt_time((hour + 1) % 24, 0),
            rotation=rotation if (rotation or playlist) else self.rotation_a, playlist=playlist,
        )


class ProfileModelTests(ScheduleFixtureMixin, TestCase):
    def setUp(self):
        self.build_fixtures()

    def test_migrated_station_has_default_schedule_active_and_default(self):
        state = ScheduleProfileState.load()
        self.assertEqual(state.pk, 1)
        self.assertEqual(state.active_profile, state.default_profile)
        self.assertEqual(state.active_profile.name, "Default Schedule")

    def test_uuid_is_stable_unique_and_not_editable(self):
        first, second = make_profile("One"), make_profile("Two")
        self.assertIsInstance(first.uuid, uuid.UUID)
        self.assertNotEqual(first.uuid, second.uuid)
        original = first.uuid
        first.name = "Renamed"
        first.save()
        first.refresh_from_db()
        self.assertEqual(first.uuid, original)
        self.assertFalse(ScheduleProfile._meta.get_field("uuid").editable)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScheduleProfile.objects.filter(pk=second.pk).update(uuid=original)

    def test_name_is_unique(self):
        make_profile("Same")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_profile("Same")

    def test_schedule_block_protects_its_profile(self):
        profile = make_profile("Held")
        self.weekly(profile, 6)
        with self.assertRaises(ProtectedError):
            profile.delete()

    def test_playlist_log_provenance_protects_its_profile(self):
        profile = make_profile("Provenance")
        PlaylistLog.objects.create(date=MONDAY, hour=3, schedule_profile=profile)
        with self.assertRaises(ProtectedError):
            profile.delete()

    def test_state_pointers_protect_their_profiles(self):
        with self.assertRaises(ProtectedError):
            active_profile().delete()

    def test_state_save_always_uses_the_singleton_pk(self):
        state = ScheduleProfileState.load()
        extra = ScheduleProfileState(active_profile=state.active_profile, default_profile=state.default_profile)
        extra.save()
        self.assertEqual(extra.pk, 1)
        self.assertEqual(ScheduleProfileState.objects.count(), 1)

    def test_active_and_default_are_independent_pointers(self):
        other = make_profile("Other")
        state = ScheduleProfileState.load()
        state.active_profile = other
        state.save()
        state = ScheduleProfileState.load()
        self.assertEqual(state.active_profile, other)
        self.assertEqual(state.default_profile.name, "Default Schedule")
        self.assertEqual(ScheduleProfile._meta.get_field("is_archived").default, False)
        self.assertFalse(hasattr(ScheduleProfile, "is_active"))


class ScheduleProfileStateRecoveryTests(ScheduleFixtureMixin, TestCase):
    """ScheduleProfileState.load() fails closed instead of guessing."""

    def setUp(self):
        self.build_fixtures()

    def drop_state(self):
        ScheduleProfileState.objects.all().delete()
        self.assertFalse(ScheduleProfileState.objects.exists())

    def test_existing_state_is_returned_unchanged(self):
        state = ScheduleProfileState.load()
        other = make_profile("Other")
        state.active_profile = other
        state.save()
        loaded = ScheduleProfileState.load()
        self.assertEqual((loaded.active_profile, loaded.default_profile.name), (other, "Default Schedule"))
        self.assertEqual(ScheduleProfileState.objects.count(), 1)

    def test_missing_state_with_exactly_one_profile_is_recreated_from_it(self):
        sole = ScheduleProfile.objects.get()
        self.drop_state()
        state = ScheduleProfileState.load()
        self.assertEqual((state.pk, state.active_profile, state.default_profile), (1, sole, sole))

    def test_the_sole_profile_is_used_even_if_archived(self):
        sole = ScheduleProfile.objects.get()
        ScheduleProfile.objects.filter(pk=sole.pk).update(is_archived=True, sort_order=99)
        self.drop_state()
        state = ScheduleProfileState.load()
        self.assertEqual((state.active_profile_id, state.default_profile_id), (sole.pk, sole.pk))

    def test_missing_state_with_multiple_profiles_fails_closed_without_guessing(self):
        default = ScheduleProfile.objects.get()
        # Variants a heuristic could latch onto: lowest sort_order, first
        # alphabetically, archived flag, lowest pk. None may be chosen.
        early = ScheduleProfile.objects.create(name="AAA First Alphabetically", sort_order=-50)
        archived = ScheduleProfile.objects.create(name="ZZZ Archived", is_archived=True)
        self.drop_state()
        before = list(ScheduleProfile.objects.order_by("pk").values_list("pk", "name", "sort_order", "is_archived"))
        with self.assertRaisesRegex(ScheduleProfileStateError, "more than one schedule profile exists"):
            ScheduleProfileState.load()
        self.assertFalse(ScheduleProfileState.objects.exists())
        self.assertEqual(
            list(ScheduleProfile.objects.order_by("pk").values_list("pk", "name", "sort_order", "is_archived")), before,
        )
        self.assertEqual({default.pk, early.pk, archived.pk}, {row[0] for row in before})

    def test_missing_state_with_no_profiles_fails_closed_and_manufactures_nothing(self):
        self.drop_state()
        ScheduleProfile.objects.all().delete()
        with self.assertRaisesRegex(ScheduleProfileStateError, "no schedule profile exists"):
            ScheduleProfileState.load()
        self.assertFalse(ScheduleProfile.objects.exists())
        self.assertFalse(ScheduleProfileState.objects.exists())

    def test_resolver_and_builders_fail_closed_when_state_is_ambiguous(self):
        default = ScheduleProfile.objects.get()
        make_profile("Second")
        self.drop_state()
        self.dated(default, 8, MONDAY)
        for label, call in (
            ("active profile", lambda: get_active_schedule_profile()),
            ("resolve (implicit)", lambda: resolve_schedule_block(MONDAY, 8)),
            ("build", lambda: build_hour_log(MONDAY, 8)),
            ("admin rebuild", lambda: build_hour_log_for_admin(MONDAY, 8)),
            ("preview", lambda: preview_hour_log(MONDAY, 8)),
        ):
            with self.subTest(call=label), self.assertRaises(ScheduleProfileStateError):
                call()
        self.assertFalse(PlaylistLog.objects.exists())
        self.assertFalse(ScheduleProfileState.objects.exists())
        # An explicitly supplied profile does not need the state row at all.
        self.assertIsNotNone(resolve_schedule_block(MONDAY, 8, profile=default))

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_schedule_api_fails_closed_when_state_is_ambiguous(self):
        make_profile("Second")
        self.drop_state()
        admin = User.objects.create_superuser("recovery_admin", "recovery@example.invalid", "pw")
        self.client.force_login(admin)
        url = reverse("library:api-schedule-list")
        with self.assertRaises(ScheduleProfileStateError):
            self.client.get(url)
        with self.assertRaises(ScheduleProfileStateError):
            self.client.post(url, data=json.dumps({
                "day_of_week": 1, "hour": 5, "rotation_id": self.rotation_a.id,
            }), content_type="application/json")
        self.assertFalse(ScheduleBlock.objects.exists())
        self.assertFalse(ScheduleProfileState.objects.exists())

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_admin_views_neither_recover_state_nor_crash_when_it_is_missing(self):
        make_profile("Second")
        self.drop_state()
        admin = User.objects.create_superuser("recovery_admin2", "recovery2@example.invalid", "pw")
        self.client.force_login(admin)
        profiles = self.client.get(reverse("admin:library_scheduleprofile_changelist"))
        self.assertEqual(profiles.status_code, 200)
        self.assertIn("state missing", profiles.content.decode())
        self.assertEqual(self.client.get(reverse("admin:library_scheduleprofilestate_changelist")).status_code, 200)
        add = self.client.get(reverse("admin:library_scheduleblock_add"))
        self.assertEqual(add.status_code, 200)
        self.assertNotIn("profile", add.context["adminform"].form.initial)
        self.assertFalse(ScheduleProfileState.objects.exists())


class ScheduleBlockConstraintTests(ScheduleFixtureMixin, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.profile_a = make_profile("A")
        self.profile_b = make_profile("B")

    def test_duplicate_recurring_slot_in_one_profile_is_rejected(self):
        self.weekly(self.profile_a, 9)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.weekly(self.profile_a, 9, rotation=self.rotation_b)

    def test_duplicate_dated_slot_in_one_profile_is_rejected(self):
        self.dated(self.profile_a, 9, MONDAY)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.dated(self.profile_a, 9, MONDAY, rotation=self.rotation_b)

    def test_same_weekday_and_time_is_allowed_in_two_profiles(self):
        self.weekly(self.profile_a, 9)
        self.weekly(self.profile_b, 9)
        self.assertEqual(ScheduleBlock.objects.filter(day_of_week=0, start_time=dt_time(9, 0)).count(), 2)

    def test_same_date_and_time_is_allowed_in_two_profiles(self):
        self.dated(self.profile_a, 9, MONDAY)
        self.dated(self.profile_b, 9, MONDAY)
        self.assertEqual(ScheduleBlock.objects.filter(specific_date=MONDAY).count(), 2)

    def test_recurring_and_dated_rows_may_share_a_time_in_one_profile(self):
        self.weekly(self.profile_a, 9)
        self.dated(self.profile_a, 9, MONDAY)
        self.assertEqual(ScheduleBlock.objects.filter(profile=self.profile_a).count(), 2)

    def test_uniqueness_is_by_exact_start_time_not_hour(self):
        """Keeps several blocks inside one hour representable later."""
        self.weekly(self.profile_a, 9)
        ScheduleBlock.objects.create(
            profile=self.profile_a, day_of_week=0, start_time=dt_time(9, 30),
            end_time=dt_time(10, 0), rotation=self.rotation_b,
        )
        self.assertEqual(ScheduleBlock.objects.filter(profile=self.profile_a, day_of_week=0).count(), 2)

    def test_profile_is_required(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScheduleBlock.objects.create(
                day_of_week=0, start_time=dt_time(1, 0), end_time=dt_time(2, 0), rotation=self.rotation_a,
            )


class ProfileResolutionTests(ScheduleFixtureMixin, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.profile_a = make_profile("A")
        self.profile_b = make_profile("B")

    def test_recurring_block_resolves_within_the_selected_profile(self):
        block = self.weekly(self.profile_a, 10)
        self.assertEqual(resolve_schedule_block(MONDAY, 10, profile=self.profile_a), block)

    def test_specific_date_beats_recurring_in_the_same_profile(self):
        self.weekly(self.profile_a, 10)
        override = self.dated(self.profile_a, 10, MONDAY, rotation=self.rotation_b)
        self.assertEqual(resolve_schedule_block(MONDAY, 10, profile=self.profile_a), override)
        # A different date still sees the recurring block.
        recurring = ScheduleBlock.objects.get(profile=self.profile_a, day_of_week=0)
        self.assertIsNone(resolve_schedule_block(TUESDAY, 10, profile=self.profile_a))
        self.assertEqual(resolve_schedule_block(date(2027, 3, 8), 10, profile=self.profile_a), recurring)

    def test_specific_date_in_a_does_not_affect_b(self):
        self.dated(self.profile_a, 10, MONDAY, rotation=self.rotation_b)
        recurring_b = self.weekly(self.profile_b, 10)
        self.assertEqual(resolve_schedule_block(MONDAY, 10, profile=self.profile_b), recurring_b)

    def test_missing_block_in_a_does_not_fall_through_to_b(self):
        self.weekly(self.profile_b, 10)
        self.assertIsNone(resolve_schedule_block(MONDAY, 10, profile=self.profile_a))

    def test_omitted_profile_uses_the_active_profile(self):
        default_block = self.weekly(active_profile(), 11)
        other_block = self.weekly(self.profile_a, 11, rotation=self.rotation_b)
        self.assertEqual(resolve_schedule_block(MONDAY, 11), default_block)
        state = ScheduleProfileState.load()
        state.active_profile = self.profile_a
        state.save()
        self.assertEqual(resolve_schedule_block(MONDAY, 11), other_block)
        self.assertEqual(get_active_schedule_profile(), self.profile_a)

    def test_only_exact_hour_start_resolves_as_before(self):
        ScheduleBlock.objects.create(
            profile=self.profile_a, day_of_week=0, start_time=dt_time(10, 30),
            end_time=dt_time(11, 0), rotation=self.rotation_a,
        )
        self.assertIsNone(resolve_schedule_block(MONDAY, 10, profile=self.profile_a))


class ProvenanceTests(ScheduleFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        kind = CategoryKind.objects.create(code="prov-test", name="Provenance Test")
        self.category = Category.objects.create(code="PROVTEST", name="Provenance", kind=kind)
        self.artist = Artist.objects.create(name="Provenance Artist")
        self.build_fixtures()
        self.profile_a = make_profile("A")
        self.profile_b = make_profile("B")
        self.playlist_a = self.playlist_with_track("A")
        self.playlist_b = self.playlist_with_track("B")

    def playlist_with_track(self, label):
        path = Path(self.tempdir.name) / f"{label}.wav"
        path.touch()
        track = Track.objects.create(
            filepath=str(path), filename=path.name, title=f"Track {label}", artist=self.artist,
            category=self.category, ready2air=True, duration_seconds=3600.0, next_start_seconds=3600.0,
        )
        playlist = Playlist.objects.create(name=f"Playlist {label}")
        PlaylistItem.objects.create(playlist=playlist, position=0, track=track)
        return playlist

    def use_profile(self, profile):
        state = ScheduleProfileState.load()
        state.active_profile = profile
        state.save()

    def test_new_log_records_the_profile_used(self):
        self.dated(self.profile_a, 8, MONDAY, playlist=self.playlist_a)
        self.use_profile(self.profile_a)
        log, error = build_hour_log(MONDAY, 8)
        self.assertIsNone(error)
        self.assertEqual(log.schedule_profile, self.profile_a)
        log.refresh_from_db()
        self.assertEqual(log.schedule_profile_id, self.profile_a.pk)
        self.assertEqual(log.items.get().track_title, "Track A")

    def test_changing_active_pointer_does_not_change_existing_provenance(self):
        self.dated(self.profile_a, 8, MONDAY, playlist=self.playlist_a)
        self.use_profile(self.profile_a)
        log, _ = build_hour_log(MONDAY, 8)
        self.use_profile(self.profile_b)
        log.refresh_from_db()
        self.assertEqual(log.schedule_profile, self.profile_a)

    def test_explicit_profile_is_used_for_resolution_and_provenance(self):
        self.weekly(self.profile_a, 8, playlist=self.playlist_a)
        self.weekly(self.profile_b, 8, playlist=self.playlist_b)
        self.use_profile(self.profile_a)
        log, error = build_hour_log(MONDAY, 8, schedule_profile=self.profile_b)
        self.assertIsNone(error)
        self.assertEqual(log.schedule_profile, self.profile_b)
        self.assertEqual(log.items.get().track_title, "Track B")

    def test_a_build_captures_the_active_profile_exactly_once(self):
        """A concurrent activation between two reads must not split a build."""
        self.weekly(self.profile_a, 8, playlist=self.playlist_a)
        self.weekly(self.profile_b, 8, playlist=self.playlist_b)
        self.use_profile(self.profile_a)
        real = log_builder.get_active_schedule_profile
        calls = []

        def flip_after_first_read():
            profile = real()
            calls.append(profile)
            self.use_profile(self.profile_b)
            return profile

        with patch.object(log_builder, "get_active_schedule_profile", side_effect=flip_after_first_read):
            log, error = build_hour_log(MONDAY, 8)
        self.assertIsNone(error)
        self.assertEqual(len(calls), 1)
        self.assertEqual(log.schedule_profile, self.profile_a)
        self.assertEqual(log.items.get().track_title, "Track A")

    def test_direct_playlist_build_records_no_profile(self):
        """'Play this playlist now' is not schedule resolution: NULL, not a guess."""
        log, error = _build_from_playlist(MONDAY, 8, self.playlist_a)
        self.assertIsNone(error)
        self.assertIsNone(log.schedule_profile)

    def test_legacy_logs_keep_null_profile(self):
        legacy = PlaylistLog.objects.create(date=MONDAY, hour=2, status="approved")
        self.assertIsNone(legacy.schedule_profile)
        self.use_profile(self.profile_b)
        legacy.refresh_from_db()
        self.assertIsNone(legacy.schedule_profile)

    def test_one_log_per_date_and_hour_is_unchanged(self):
        PlaylistLog.objects.create(date=MONDAY, hour=5, schedule_profile=self.profile_a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlaylistLog.objects.create(date=MONDAY, hour=5, schedule_profile=self.profile_b)

    def test_admin_rebuild_replaces_the_hour_and_records_the_rebuild_profile(self):
        self.weekly(self.profile_a, 8, playlist=self.playlist_a)
        self.weekly(self.profile_b, 8, playlist=self.playlist_b)
        self.use_profile(self.profile_a)
        first, _ = build_hour_log_for_admin(MONDAY, 8)
        self.assertEqual(first.status, "draft")
        self.use_profile(self.profile_b)
        second, error = build_hour_log_for_admin(MONDAY, 8)
        self.assertIsNone(error)
        self.assertEqual(second.status, "draft")
        self.assertEqual(PlaylistLog.objects.filter(date=MONDAY, hour=8).count(), 1)
        self.assertEqual(PlaylistLog.objects.get(date=MONDAY, hour=8).schedule_profile, self.profile_b)

    def test_engine_style_build_reuses_an_approved_log_unchanged(self):
        self.weekly(self.profile_a, 8, playlist=self.playlist_a)
        self.weekly(self.profile_b, 8, playlist=self.playlist_b)
        self.use_profile(self.profile_a)
        built, error = build_and_approve_hour_log_locked(MONDAY, 8)
        self.assertIsNone(error)
        self.assertEqual(built.status, "approved")
        self.use_profile(self.profile_b)
        reused, error = build_and_approve_hour_log_locked(MONDAY, 8)
        self.assertIsNone(error)
        self.assertEqual(reused.pk, built.pk)
        reused.refresh_from_db()
        self.assertEqual(reused.schedule_profile, self.profile_a)
        self.assertEqual(reused.items.get().track_title, "Track A")

    def test_advisory_lock_contention_is_still_reported(self):
        self.weekly(self.profile_a, 8, playlist=self.playlist_a)
        self.use_profile(self.profile_a)
        with patch.object(log_builder, "_advisory_lock_for_hour") as lock:
            lock.return_value.__enter__.return_value = False
            log, error = build_hour_log_for_admin(MONDAY, 8)
        self.assertIsNone(log)
        self.assertEqual(error, log_builder.LOCK_CONTENDED)
        self.assertFalse(PlaylistLog.objects.filter(date=MONDAY, hour=8).exists())

    def test_preview_uses_the_explicit_profile_and_persists_nothing(self):
        self.weekly(self.profile_b, 8, playlist=self.playlist_b)
        self.use_profile(self.profile_a)
        result, _ = preview_hour_log(MONDAY, 8)
        self.assertEqual(result["source"], None)
        result, error = preview_hour_log(MONDAY, 8, schedule_profile=self.profile_b)
        self.assertIsNone(error)
        self.assertEqual(result["source_name"], "Playlist B")
        self.assertFalse(PlaylistLog.objects.exists())

    def test_a_missing_block_in_the_profile_never_builds_from_another_profile(self):
        self.weekly(self.profile_b, 8, playlist=self.playlist_b)
        self.use_profile(self.profile_a)
        log, error = build_hour_log(MONDAY, 8)
        self.assertIsNone(log)
        self.assertEqual(error, "No schedule block for this hour.")


@override_settings(SECURE_SSL_REDIRECT=False)
class ScheduleApiCompatibilityTests(ScheduleFixtureMixin, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.default = active_profile()
        self.other = make_profile("Other")
        group = Group.objects.create(name="3.1A Schedule Editors")
        GroupAccess.objects.create(group=group, allowed_prefixes="/api/")
        role = Role.objects.create(name="3.1A Schedule Editor")
        RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug="schedule.edit"))
        GroupRole.objects.create(group=group, role=role)
        self.editor = User.objects.create_user("editor_3_1a", password="pw")
        self.editor.groups.add(group)
        reach = Group.objects.create(name="3.1A Reach Only")
        GroupAccess.objects.create(group=reach, allowed_prefixes="/api/")
        self.reader = User.objects.create_user("reader_3_1a", password="pw")
        self.reader.groups.add(reach)
        self.url = reverse("library:api-schedule-list")

    def post(self, **payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def test_get_returns_the_active_profile_recurring_schedule_in_the_r0095_shape(self):
        block = self.weekly(self.default, 7, dow=2, rotation=self.rotation_a)
        self.weekly(self.other, 7, dow=2, rotation=self.rotation_b)
        self.dated(self.default, 7, MONDAY)
        self.client.force_login(self.reader)
        payload = self.client.get(self.url).json()
        # r0095 shape plus the additive 3.1C `start_minute` key (0 for an hourly row), which
        # lets clients tell minute transitions from the :00 base; no existing key changed.
        self.assertEqual(payload, {"blocks": [{
            "id": block.id, "day_of_week": 2, "start_hour": 7, "start_minute": 0,
            "content_kind": "rotation",
            "content_id": self.rotation_a.id, "content_name": "Rotation A",
        }]})

    def test_post_creates_then_updates_the_active_profile_block(self):
        self.client.force_login(self.editor)
        created = self.post(day_of_week=1, hour=5, rotation_id=self.rotation_a.id).json()
        self.assertTrue(created["created"])
        block = ScheduleBlock.objects.get(pk=created["id"])
        self.assertEqual(block.profile, self.default)
        self.assertEqual(block.end_time, dt_time(6, 0))
        updated = self.post(day_of_week=1, hour=5, rotation_id=self.rotation_b.id).json()
        self.assertFalse(updated["created"])
        self.assertEqual(updated["id"], created["id"])
        block.refresh_from_db()
        self.assertEqual(block.rotation, self.rotation_b)

    def test_post_supports_playlists_and_keeps_validation_messages(self):
        self.client.force_login(self.editor)
        ok = self.post(day_of_week=1, hour=5, playlist_id=self.playlist.id)
        self.assertEqual(ok.json()["content_kind"], "playlist")
        self.assertEqual(self.post(day_of_week=1, hour=5).status_code, 400)
        self.assertEqual(self.post(day_of_week=9, hour=5, rotation_id=self.rotation_a.id).status_code, 400)
        self.assertEqual(self.post(day_of_week=1, hour=5, rotation_id=999999).status_code, 404)

    def test_post_never_touches_another_profiles_identical_slot(self):
        other_block = self.weekly(self.other, 5, dow=1, rotation=self.rotation_b)
        self.client.force_login(self.editor)
        self.post(day_of_week=1, hour=5, rotation_id=self.rotation_a.id)
        other_block.refresh_from_db()
        self.assertEqual(other_block.rotation, self.rotation_b)
        self.assertEqual(ScheduleBlock.objects.filter(day_of_week=1, start_time=dt_time(5, 0)).count(), 2)

    def test_post_follows_the_active_profile(self):
        state = ScheduleProfileState.load()
        state.active_profile = self.other
        state.save()
        self.client.force_login(self.editor)
        block = ScheduleBlock.objects.get(pk=self.post(day_of_week=3, hour=4, rotation_id=self.rotation_a.id).json()["id"])
        self.assertEqual(block.profile, self.other)

    def test_delete_is_scoped_to_the_active_profile(self):
        mine = self.weekly(self.default, 9)
        theirs = self.weekly(self.other, 9)
        self.client.force_login(self.editor)
        blocked = self.client.delete(reverse("library:api-schedule-delete", args=[theirs.pk]))
        self.assertEqual(blocked.json(), {"ok": True, "deleted": False})
        self.assertTrue(ScheduleBlock.objects.filter(pk=theirs.pk).exists())
        allowed = self.client.delete(reverse("library:api-schedule-delete", args=[mine.pk]))
        self.assertEqual(allowed.json(), {"ok": True, "deleted": True})
        self.assertFalse(ScheduleBlock.objects.filter(pk=mine.pk).exists())

    def test_schedule_edit_capability_is_still_required(self):
        self.client.force_login(self.reader)
        block = self.weekly(self.default, 9)
        self.assertEqual(self.post(day_of_week=1, hour=5, rotation_id=self.rotation_a.id).status_code, 403)
        self.assertEqual(self.client.delete(reverse("library:api-schedule-delete", args=[block.pk])).status_code, 403)
        self.assertTrue(ScheduleBlock.objects.filter(pk=block.pk).exists())


@override_settings(SECURE_SSL_REDIRECT=False)
class ScheduleProfileAdminTests(ScheduleFixtureMixin, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.superuser = User.objects.create_superuser("admin_3_1a", "admin_3_1a@example.invalid", "pw")
        self.client.force_login(self.superuser)

    def test_changelists_render_with_profile_columns_and_filters(self):
        profile = active_profile()
        self.weekly(profile, 6)
        PlaylistLog.objects.create(date=MONDAY, hour=1, schedule_profile=profile)
        for name in ("scheduleprofile", "scheduleprofilestate", "scheduleblock", "playlistlog"):
            with self.subTest(model=name):
                response = self.client.get(reverse(f"admin:library_{name}_changelist"))
                self.assertEqual(response.status_code, 200)
        listing = self.client.get(reverse("admin:library_scheduleprofile_changelist")).content.decode()
        self.assertIn("ACTIVE, DEFAULT", listing)

    def test_active_and_default_pointers_cannot_be_edited_added_or_deleted_in_admin(self):
        state = ScheduleProfileState.load()
        self.assertEqual(self.client.get(reverse("admin:library_scheduleprofilestate_add")).status_code, 403)
        self.assertEqual(self.client.post(
            reverse("admin:library_scheduleprofilestate_delete", args=[state.pk]), {"post": "yes"},
        ).status_code, 403)
        self.assertEqual(self.client.post(
            reverse("admin:library_scheduleprofilestate_change", args=[state.pk]),
            {"active_profile": make_profile("X").pk, "default_profile": state.default_profile_id},
        ).status_code, 403)
        state.refresh_from_db()
        self.assertEqual(state.active_profile.name, "Default Schedule")

    def test_profiles_cannot_be_deleted_or_archived_from_admin(self):
        profile = active_profile()
        self.assertEqual(self.client.post(
            reverse("admin:library_scheduleprofile_delete", args=[profile.pk]), {"post": "yes"},
        ).status_code, 403)
        change = reverse("admin:library_scheduleprofile_change", args=[profile.pk])
        self.client.post(change, {"name": profile.name, "description": "", "sort_order": 0, "is_archived": "on"})
        profile.refresh_from_db()
        self.assertFalse(profile.is_archived)

    def test_new_schedule_block_form_defaults_to_the_active_profile(self):
        response = self.client.get(reverse("admin:library_scheduleblock_add"))
        self.assertEqual(response.context["adminform"].form.initial["profile"], active_profile().pk)


class ScheduleProfileMigrationTests(TransactionTestCase):
    """Runs the real 0086 -> 0087 -> 0088 sequence against the test DB."""

    before = [("library", "0085_remote_dj_queue_set_next_access")]
    stage1 = [("library", "0086_scheduleprofile_foundation_schema")]
    final = [("library", "0088_enforce_schedule_profile_integrity")]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def setUp(self):
        self.addCleanup(self.restore_latest)

    def restore_latest(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def seed_r0095_schedule(self, apps):
        Rotation = apps.get_model("library", "Rotation")
        Playlist = apps.get_model("library", "Playlist")
        Block = apps.get_model("library", "ScheduleBlock")
        Log = apps.get_model("library", "PlaylistLog")
        rotation = Rotation.objects.create(name="Migrated Rotation")
        playlist = Playlist.objects.create(name="Migrated Playlist")
        rows = {
            "weekly_rotation": Block.objects.create(
                day_of_week=0, start_time=dt_time(6, 0), end_time=dt_time(7, 0), rotation=rotation),
            "weekly_playlist": Block.objects.create(
                day_of_week=4, start_time=dt_time(20, 0), end_time=dt_time(21, 0), playlist=playlist),
            "dated_override": Block.objects.create(
                specific_date=MONDAY, start_time=dt_time(6, 0), end_time=dt_time(7, 0), playlist=playlist),
        }
        Log.objects.create(date=MONDAY, hour=6, status="approved")
        return rows

    def test_backfill_assigns_every_block_and_sets_active_and_default(self):
        old_apps = self.migrate(self.before)
        rows = self.seed_r0095_schedule(old_apps)
        before = {
            key: (b.day_of_week, b.specific_date, b.start_time, b.end_time, b.rotation_id, b.playlist_id)
            for key, b in rows.items()
        }
        self.migrate(self.final)

        profiles = ScheduleProfile.objects.all()
        self.assertEqual([p.name for p in profiles], ["Default Schedule"])
        default = profiles[0]
        for key, old in rows.items():
            block = ScheduleBlock.objects.get(pk=old.pk)
            self.assertEqual(block.profile, default, key)
            self.assertEqual(
                (block.day_of_week, block.specific_date, block.start_time, block.end_time,
                 block.rotation_id, block.playlist_id), before[key], key,
            )
        self.assertEqual(ScheduleBlock.objects.count(), len(rows))
        state = ScheduleProfileState.objects.get()
        self.assertEqual((state.pk, state.active_profile, state.default_profile), (1, default, default))

        # Resolution is exactly what r0095 would have produced.
        self.assertEqual(resolve_schedule_block(MONDAY, 6).pk, rows["dated_override"].pk)
        self.assertEqual(resolve_schedule_block(date(2027, 3, 8), 6).pk, rows["weekly_rotation"].pk)
        self.assertEqual(resolve_schedule_block(date(2027, 3, 5), 20).pk, rows["weekly_playlist"].pk)
        self.assertIsNone(resolve_schedule_block(TUESDAY, 6))

        legacy = PlaylistLog.objects.get(date=MONDAY, hour=6)
        self.assertIsNone(legacy.schedule_profile)
        self.assertEqual(legacy.status, "approved")

    def test_an_empty_schedule_still_gets_a_default_profile_and_state(self):
        self.migrate(self.before)
        self.migrate(self.final)
        state = ScheduleProfileState.objects.get()
        self.assertEqual(state.active_profile.name, "Default Schedule")
        self.assertEqual(ScheduleBlock.objects.count(), 0)

    def test_ambiguous_existing_rows_abort_the_backfill_without_changing_anything(self):
        apps = self.migrate(self.stage1)
        Rotation = apps.get_model("library", "Rotation")
        Block = apps.get_model("library", "ScheduleBlock")
        rotation = Rotation.objects.create(name="Dup Rotation")
        first = Block.objects.create(day_of_week=2, start_time=dt_time(8, 0), end_time=dt_time(9, 0), rotation=rotation)
        second = Block.objects.create(day_of_week=2, start_time=dt_time(8, 0), end_time=dt_time(9, 0), rotation=rotation)

        with self.assertRaises(RuntimeError) as raised:
            self.migrate([("library", "0087_backfill_default_schedule_profile")])
        message = str(raised.exception)
        self.assertIn("day_of_week=2", message)
        self.assertIn(str(first.pk), message)
        self.assertIn(str(second.pk), message)

        self.assertEqual(ScheduleProfile.objects.count(), 0)
        self.assertEqual(ScheduleProfileState.objects.count(), 0)
        self.assertEqual(Block.objects.count(), 2)
        self.assertEqual(Block.objects.filter(profile__isnull=False).count(), 0)

        # The operator resolves the duplicate manually; the migration then proceeds.
        Block.objects.filter(pk=second.pk).delete()
        self.migrate(self.final)
        self.assertEqual(ScheduleBlock.objects.get().profile.name, "Default Schedule")

    def test_ambiguous_specific_date_rows_also_abort(self):
        apps = self.migrate(self.stage1)
        Rotation = apps.get_model("library", "Rotation")
        Block = apps.get_model("library", "ScheduleBlock")
        rotation = Rotation.objects.create(name="Dup Rotation")
        for _ in range(2):
            Block.objects.create(specific_date=MONDAY, start_time=dt_time(8, 0), end_time=dt_time(9, 0), rotation=rotation)
        with self.assertRaisesRegex(RuntimeError, "specific-date specific_date=2027-03-01"):
            self.migrate([("library", "0087_backfill_default_schedule_profile")])
        self.assertEqual(ScheduleProfile.objects.count(), 0)
        Block.objects.all().delete()  # let the cleanup migrate forward to the latest state
