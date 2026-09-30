"""Regression coverage for the 3.1B operator workflow."""
import json
from datetime import date, time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability
from library.models import (
    GroupAccess, LogItem, PlaylistLog, ScheduleBlock, ScheduleProfile,
    ScheduleProfileState,
)
from library.services.schedule_profiles import clone_profile
from library.services.log_builder import resolve_schedule_block
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.tests.test_schedule_profiles_3_1a import ScheduleFixtureMixin
from monitoring.models import SystemEvent


User = get_user_model()
MONDAY = date(2027, 3, 1)


class ApiMixin(ScheduleFixtureMixin):
    def setUp(self):
        self.build_fixtures()
        self.state = ensure_schedule_profile_state()
        self.default = self.state.active_profile
        group = Group.objects.create(name=f"3.1B editors {self.__class__.__name__}")
        GroupAccess.objects.create(group=group, allowed_prefixes="/schedule/\n/api/")
        role = Role.objects.create(name=f"3.1B editor {self.__class__.__name__}")
        capability, _ = Capability.objects.get_or_create(
            slug="schedule.edit", defaults={"label": "Edit schedule"},
        )
        RoleCapability.objects.create(role=role, capability=capability)
        GroupRole.objects.create(group=group, role=role)
        self.editor = User.objects.create_user(f"editor_{self.__class__.__name__}", password="pw")
        self.editor.groups.add(group)
        reader_group = Group.objects.create(name=f"3.1B readers {self.__class__.__name__}")
        GroupAccess.objects.create(group=reader_group, allowed_prefixes="/schedule/\n/api/")
        self.reader = User.objects.create_user(f"reader_{self.__class__.__name__}", password="pw")
        self.reader.groups.add(reader_group)
        self.client.force_login(self.editor)

    def profiles_url(self):
        return reverse("library:api-schedule-profiles")

    def detail_url(self, profile):
        return reverse("library:api-schedule-profile-detail", args=[profile.uuid])

    def action_url(self, profile, action):
        return reverse("library:api-schedule-profile-action", args=[profile.uuid, action])

    def post_json(self, url, payload):
        return self.client.post(url, json.dumps(payload), content_type="application/json")

    def patch_json(self, url, payload):
        return self.client.patch(url, json.dumps(payload), content_type="application/json")

    def create_profile(self, name="Test Profile"):
        response = self.post_json(self.profiles_url(), {"name": name, "description": "Prepared off air"})
        self.assertEqual(response.status_code, 201, response.content)
        return ScheduleProfile.objects.get(uuid=response.json()["uuid"])


@override_settings(SECURE_SSL_REDIRECT=False)
class ProfileLifecycleApiTests(ApiMixin, TestCase):
    def test_schedule_page_exposes_profile_lifecycle_and_date_override_workflow(self):
        response = self.client.get(reverse("library:schedule"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for marker in (
            'id="profileSelect"', "New Profile", "Activate", "Set Default",
            "Weekly Schedule", "Date Override", "Revert to Weekly",
            "expected_active_profile_uuid",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)

    def test_create_lists_profile_without_activating_or_defaulting_it(self):
        profile = self.create_profile()
        self.state.refresh_from_db()
        self.assertNotEqual(profile, self.state.active_profile)
        self.assertNotEqual(profile, self.state.default_profile)
        self.assertFalse(profile.is_archived)
        payload = self.client.get(self.profiles_url()).json()
        row = next(p for p in payload["profiles"] if p["uuid"] == str(profile.uuid))
        self.assertFalse(row["is_active"])
        self.assertFalse(row["is_default"])
        self.assertTrue(SystemEvent.objects.filter(category="schedule", title="Schedule profile created").exists())

    def test_duplicate_name_is_clean_validation_error(self):
        self.create_profile("Duplicate")
        response = self.post_json(self.profiles_url(), {"name": "Duplicate"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.json()["error"])

    def test_metadata_edit_does_not_change_uuid_or_state(self):
        profile = self.create_profile()
        old_uuid = profile.uuid
        response = self.patch_json(self.detail_url(profile), {
            "name": "Renamed", "description": "New description", "sort_order": 42,
        })
        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertEqual(profile.uuid, old_uuid)
        self.assertEqual((profile.name, profile.description, profile.sort_order), ("Renamed", "New description", 42))
        self.assertEqual(self.patch_json(self.detail_url(profile), {"uuid": str(self.default.uuid)}).status_code, 400)
        self.state.refresh_from_db()
        self.assertEqual(self.state.active_profile, self.default)
        self.assertEqual(self.state.default_profile, self.default)

    def test_clone_uses_new_uuid_copies_exact_recurring_rows_and_shares_content(self):
        source = self.create_profile("Source")
        weekly = ScheduleBlock.objects.create(
            profile=source, day_of_week=2, start_time=time(10, 30), end_time=time(11, 17), rotation=self.rotation_a,
        )
        ScheduleBlock.objects.create(
            profile=source, specific_date=MONDAY, start_time=time(12), end_time=time(13), playlist=self.playlist,
        )
        PlaylistLog.objects.create(date=MONDAY, hour=1, schedule_profile=source)
        response = self.post_json(self.action_url(source, "clone"), {"name": "Clone"})
        self.assertEqual(response.status_code, 201, response.content)
        clone = ScheduleProfile.objects.get(uuid=response.json()["uuid"])
        self.assertNotEqual(clone.uuid, source.uuid)
        copied = clone.schedule_blocks.get()
        self.assertEqual((copied.day_of_week, copied.start_time, copied.end_time), (2, weekly.start_time, weekly.end_time))
        self.assertEqual(copied.rotation_id, weekly.rotation_id)
        self.assertFalse(clone.playlist_logs.exists())
        self.assertEqual(ScheduleProfileState.load().active_profile, self.default)
        audit = SystemEvent.objects.get(title="Schedule profile cloned")
        self.assertEqual(audit.detail["source_profile_uuid"], str(source.uuid))
        self.assertEqual(audit.detail["new_profile_uuid"], str(clone.uuid))
        self.assertEqual(audit.detail["recurring_blocks_copied"], 1)
        self.assertEqual(audit.detail["date_blocks_copied"], 0)

    def test_clone_can_include_date_overrides_and_clone_from_archive(self):
        source = self.create_profile("Archived source")
        ScheduleBlock.objects.create(
            profile=source, day_of_week=1, start_time=time(3), end_time=time(4), rotation=self.rotation_a,
        )
        ScheduleBlock.objects.create(
            profile=source, specific_date=MONDAY, start_time=time(3), end_time=time(4), rotation=self.rotation_b,
        )
        source.is_archived = True
        source.save(update_fields=["is_archived"])
        response = self.post_json(self.action_url(source, "clone"), {
            "name": "With dates", "include_date_overrides": True,
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ScheduleProfile.objects.get(uuid=response.json()["uuid"]).schedule_blocks.count(), 2)

    def test_clone_rolls_back_profile_when_block_copy_fails(self):
        source = self.create_profile("Rollback source")
        self.weekly(source, 4)
        with patch("library.services.schedule_profiles.ScheduleBlock.objects.bulk_create", side_effect=DatabaseError("boom")):
            with self.assertRaises(DatabaseError):
                clone_profile(source, name="Must roll back", actor=self.editor)
        self.assertFalse(ScheduleProfile.objects.filter(name="Must roll back").exists())

    def test_activation_uses_expected_uuid_and_changes_only_active_pointer(self):
        target = self.create_profile("Activation target")
        next_block = self.weekly(target, 8, dow=MONDAY.weekday(), rotation=self.rotation_b)
        approved = PlaylistLog.objects.create(date=MONDAY, hour=4, status="approved", schedule_profile=self.default)
        draft = PlaylistLog.objects.create(date=MONDAY, hour=5, status="draft", schedule_profile=self.default)
        item = LogItem.objects.create(playlist_log=approved, position=0, scheduled_time="2027-03-01T04:00:00Z")
        response = self.post_json(self.action_url(target, "activate"), {
            "expected_active_profile_uuid": str(self.default.uuid),
        })
        self.assertEqual(response.status_code, 200)
        self.state.refresh_from_db()
        self.assertEqual(self.state.active_profile, target)
        self.assertEqual(self.state.default_profile, self.default)
        approved.refresh_from_db(); draft.refresh_from_db(); item.refresh_from_db()
        self.assertEqual(approved.schedule_profile, self.default)
        self.assertEqual(draft.schedule_profile, self.default)
        self.assertIsNone(item.played_at)
        self.assertEqual(resolve_schedule_block(MONDAY, 8), next_block)
        event = SystemEvent.objects.get(title="Schedule profile activated")
        self.assertEqual(event.detail["previous_active_uuid"], str(self.default.uuid))
        self.assertEqual(event.detail["new_active_uuid"], str(target.uuid))

    def test_stale_activation_is_409_and_idempotent_activation_is_clean(self):
        target = self.create_profile("Target")
        stale = self.post_json(self.action_url(target, "activate"), {
            "expected_active_profile_uuid": "00000000-0000-0000-0000-000000000000",
        })
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(ScheduleProfileState.load().active_profile, self.default)
        ok = self.post_json(self.action_url(self.default, "activate"), {
            "expected_active_profile_uuid": str(self.default.uuid),
        })
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(ok.json()["changed"])

    def test_default_can_differ_from_active_and_never_activates(self):
        target = self.create_profile("New default")
        response = self.post_json(self.action_url(target, "set-default"), {})
        self.assertEqual(response.status_code, 200)
        state = ScheduleProfileState.load()
        self.assertEqual(state.default_profile, target)
        self.assertEqual(state.active_profile, self.default)

    def test_archive_rejects_active_default_and_archived_profile_is_read_only(self):
        response = self.post_json(self.action_url(self.default, "archive"), {})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(set(response.json()["blockers"]), {"profile is active", "profile is default"})
        profile = self.create_profile("Archive me")
        self.assertEqual(self.post_json(self.action_url(profile, "archive"), {}).status_code, 200)
        self.assertEqual(self.patch_json(self.detail_url(profile), {"name": "No"}).status_code, 409)
        self.assertEqual(self.post_json(self.action_url(profile, "activate"), {
            "expected_active_profile_uuid": str(self.default.uuid),
        }).status_code, 409)
        self.assertEqual(self.post_json(self.action_url(profile, "set-default"), {}).status_code, 409)

    def test_archive_rejects_profiles_that_are_only_active_or_only_default(self):
        profile = self.create_profile("Pointer blocker")
        self.post_json(self.action_url(profile, "set-default"), {})
        default_only = self.post_json(self.action_url(profile, "archive"), {})
        self.assertEqual(default_only.status_code, 409)
        self.assertEqual(default_only.json()["blockers"], ["profile is default"])
        self.post_json(self.action_url(self.default, "set-default"), {})
        self.post_json(self.action_url(profile, "activate"), {
            "expected_active_profile_uuid": str(self.default.uuid),
        })
        active_only = self.post_json(self.action_url(profile, "archive"), {})
        self.assertEqual(active_only.status_code, 409)
        self.assertEqual(active_only.json()["blockers"], ["profile is active"])

    def test_restore_does_not_activate_or_set_default(self):
        profile = self.create_profile("Restore me")
        self.post_json(self.action_url(profile, "archive"), {})
        response = self.post_json(self.action_url(profile, "restore"), {})
        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db(); state = ScheduleProfileState.load()
        self.assertFalse(profile.is_archived)
        self.assertEqual(state.active_profile, self.default)
        self.assertEqual(state.default_profile, self.default)

    def test_hard_delete_reports_each_blocker_and_allows_truly_unused_profile(self):
        active = self.client.delete(self.detail_url(self.default))
        self.assertEqual(active.status_code, 409)
        blocked = self.create_profile("Has data")
        self.weekly(blocked, 2)
        PlaylistLog.objects.create(date=MONDAY, hour=2, schedule_profile=blocked)
        response = self.client.delete(self.detail_url(blocked))
        self.assertEqual(response.status_code, 409)
        self.assertTrue(any("schedule block" in b for b in response.json()["blockers"]))
        self.assertTrue(any("playlist log" in b for b in response.json()["blockers"]))
        unused = self.create_profile("Unused")
        self.assertEqual(self.client.delete(self.detail_url(unused)).status_code, 200)
        self.assertFalse(ScheduleProfile.objects.filter(pk=unused.pk).exists())

    def test_lifecycle_mutations_require_schedule_edit_but_reads_remain_available(self):
        profile = self.create_profile("Protected")
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.profiles_url()).status_code, 200)
        self.assertEqual(self.client.get(self.detail_url(profile)).status_code, 200)
        for method, url, body in [
            (self.client.post, self.profiles_url(), {"name": "Denied"}),
            (self.client.patch, self.detail_url(profile), {"name": "Denied"}),
            (self.client.post, self.action_url(profile, "clone"), {"name": "Denied clone"}),
            (self.client.post, self.action_url(profile, "archive"), {}),
        ]:
            response = method(url, json.dumps(body), content_type="application/json")
            self.assertEqual(response.status_code, 403)


@override_settings(SECURE_SSL_REDIRECT=False)
class SelectedProfileAndDateApiTests(ApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_profile("Inactive")
        self.url = reverse("library:api-schedule-list")

    def post_schedule(self, **payload):
        return self.post_json(self.url, payload)

    def test_backward_compatible_get_and_post_use_active_profile(self):
        mine = self.weekly(self.default, 7, rotation=self.rotation_a)
        self.weekly(self.other, 7, rotation=self.rotation_b)
        payload = self.client.get(self.url).json()
        self.assertEqual([row["id"] for row in payload["blocks"]], [mine.pk])
        created = self.post_schedule(day_of_week=2, hour=8, rotation_id=self.rotation_a.pk)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(ScheduleBlock.objects.get(pk=created.json()["id"]).profile, self.default)

    def test_explicit_profile_read_and_write_do_not_activate(self):
        block = self.weekly(self.other, 9, rotation=self.rotation_b)
        response = self.client.get(self.url, {"profile": str(self.other.uuid)})
        self.assertEqual([row["id"] for row in response.json()["blocks"]], [block.pk])
        created = self.post_schedule(
            profile_uuid=str(self.other.uuid), day_of_week=3, hour=10, playlist_id=self.playlist.pk,
        )
        self.assertEqual(ScheduleBlock.objects.get(pk=created.json()["id"]).profile, self.other)
        self.assertEqual(ScheduleProfileState.load().active_profile, self.default)

    def test_unknown_profile_is_404_and_archived_profile_rejects_writes(self):
        self.other.is_archived = True
        self.other.save(update_fields=["is_archived"])
        missing = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(self.client.get(self.url, {"profile": missing}).status_code, 404)
        self.assertEqual(self.client.get(self.url, {"profile": self.other.uuid}).status_code, 200)
        response = self.post_schedule(
            profile_uuid=str(self.other.uuid), day_of_week=1, hour=2, rotation_id=self.rotation_a.pk,
        )
        self.assertEqual(response.status_code, 409)

    def test_delete_is_scoped_to_explicit_profile(self):
        other_block = self.weekly(self.other, 5)
        url = reverse("library:api-schedule-delete", args=[other_block.pk])
        self.assertFalse(self.client.delete(url).json()["deleted"])
        self.assertTrue(ScheduleBlock.objects.filter(pk=other_block.pk).exists())
        self.assertTrue(self.client.delete(f"{url}?profile={self.other.uuid}").json()["deleted"])

    def test_effective_date_reports_inheritance_and_override_without_persisting(self):
        weekly = self.weekly(self.other, 6, dow=MONDAY.weekday(), rotation=self.rotation_a)
        before = ScheduleBlock.objects.count()
        inherited = self.client.get(self.url, {"profile": self.other.uuid, "date": MONDAY.isoformat()}).json()
        cell = inherited["cells"][6]
        self.assertEqual(cell["origin"], "weekly")
        self.assertEqual(cell["inherited_block_id"], weekly.pk)
        self.assertIsNone(cell["explicit_block_id"])
        self.assertEqual(ScheduleBlock.objects.count(), before)

        created = self.post_schedule(
            profile_uuid=str(self.other.uuid), specific_date=MONDAY.isoformat(), hour=6,
            rotation_id=self.rotation_b.pk,
        )
        self.assertEqual(created.status_code, 200)
        weekly.refresh_from_db()
        self.assertEqual(weekly.rotation, self.rotation_a)
        overridden = self.client.get(self.url, {"profile": self.other.uuid, "date": MONDAY.isoformat()}).json()["cells"][6]
        self.assertEqual(overridden["origin"], "date_override")
        self.assertEqual(overridden["effective_block"]["content_id"], self.rotation_b.pk)

    def test_date_override_is_profile_scoped_and_update_leaves_weekly_untouched(self):
        weekly = self.weekly(self.other, 11, dow=0, rotation=self.rotation_a)
        created = self.post_schedule(
            profile_uuid=str(self.other.uuid), specific_date=MONDAY.isoformat(), hour=11,
            rotation_id=self.rotation_b.pk,
        ).json()
        updated = self.post_schedule(
            profile_uuid=str(self.other.uuid), specific_date=MONDAY.isoformat(), hour=11,
            playlist_id=self.playlist.pk,
        ).json()
        self.assertEqual(created["id"], updated["id"])
        weekly.refresh_from_db()
        self.assertEqual(weekly.rotation, self.rotation_a)
        default_cell = self.client.get(self.url, {"profile": self.default.uuid, "date": MONDAY}).json()["cells"][11]
        self.assertEqual(default_cell["origin"], "none")

    def test_revert_deletes_only_dated_row_then_reveals_weekly_or_empty(self):
        weekly = self.weekly(self.other, 12, dow=0, rotation=self.rotation_a)
        override = self.dated(self.other, 12, MONDAY, rotation=self.rotation_b)
        url = reverse("library:api-schedule-delete", args=[override.pk])
        response = self.client.delete(f"{url}?profile={self.other.uuid}&date={MONDAY}")
        self.assertTrue(response.json()["deleted"])
        self.assertTrue(ScheduleBlock.objects.filter(pk=weekly.pk).exists())
        self.assertEqual(self.client.get(self.url, {"profile": self.other.uuid, "date": MONDAY}).json()["cells"][12]["origin"], "weekly")
        lonely = self.dated(self.other, 13, MONDAY, rotation=self.rotation_b)
        lonely_url = reverse("library:api-schedule-delete", args=[lonely.pk])
        self.client.delete(f"{lonely_url}?profile={self.other.uuid}&date={MONDAY}")
        self.assertEqual(self.client.get(self.url, {"profile": self.other.uuid, "date": MONDAY}).json()["cells"][13]["origin"], "none")

    def test_invalid_date_and_archived_date_writes_are_rejected(self):
        self.assertEqual(self.client.get(self.url, {"profile": self.other.uuid, "date": "not-a-date"}).status_code, 400)
        self.assertEqual(self.post_schedule(
            profile_uuid=str(self.other.uuid), specific_date="2027-99-01", hour=1, rotation_id=self.rotation_a.pk,
        ).status_code, 400)
        self.other.is_archived = True
        self.other.save(update_fields=["is_archived"])
        self.assertEqual(self.post_schedule(
            profile_uuid=str(self.other.uuid), specific_date=MONDAY.isoformat(), hour=1, rotation_id=self.rotation_a.pk,
        ).status_code, 409)

    def test_schedule_writes_still_require_capability(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.post_schedule(
            profile_uuid=str(self.other.uuid), day_of_week=1, hour=1, rotation_id=self.rotation_a.pk,
        ).status_code, 403)
