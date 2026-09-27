"""Roadmap 2.5E -- final library authorization closeout.

The 2.5D final mutation audit found three pre-existing library write
surfaces that did not yet fit the final capability taxonomy (see
PROJECT_NOTES.md's "Roadmap 2.5" section and docs/AUTHORIZATION.md's
"Remaining authorization debt"):

  1. api_library_upload had no authorization check at all before its
     Contributor category-auto-pin RESOURCE rule.
  2. api_track_detail had no authorization check at all in its general
     (non-Contributor/remote_dj) mutation branch, before its own-upload
     RESOURCE rule.
  3. api_track_autofill_related_artists authorized negatively ("allowed
     unless Contributor/remote_dj") instead of positively.

None of these were exploitable by the two seeded non-staff Groups today
-- Contributor and remote_dj were already correctly constrained by
literal group-name checks. The risk was architectural: a future Group
with widened GroupAccess reaching these URLs would have gotten the
mutation for free. This file proves:

  - the new positive capability gates (library.upload /
    library.manage_tracks) are the actual authority boundary now, not
    GroupAccess reachability;
  - the pre-existing Contributor own-upload-delete and category-pin
    RESOURCE rules are preserved unchanged (capability AND resource
    rule, never capability OR resource rule, never capability replacing
    the resource rule);
  - staff/superuser compatibility is unaffected.
"""
import json
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability
from library.models import Artist, Category, CategoryKind, GroupAccess, Track

User = get_user_model()


def make_category(code="rock", kind_code="music"):
    kind, _ = CategoryKind.objects.get_or_create(code=kind_code, defaults={"name": kind_code.title()})
    return Category.objects.create(code=code, name=code.title(), kind=kind)


def make_track(title="Test Track", category=None, uploaded_by=None, ready2air=False):
    artist, _ = Artist.get_or_create_ci("Authz 2.5E Test Artist")
    return Track.objects.create(
        filepath=f"/srv/isadoraair/music/{title}.mp3",
        filename=f"{title}.mp3",
        format="mp3",
        title=title,
        artist=artist,
        category=category,
        uploaded_by=uploaded_by,
        ready2air=ready2air,
    )


def make_capable_group(group_name, role_name, slugs, prefixes):
    """A brand-new Group with real GroupAccess reachability (proving the
    request genuinely reaches the view) but authority coming only from
    the capability -- the exact "reachability != authority" proof
    pattern established in test_authz_category_track_enforcement.py."""
    group = Group.objects.create(name=group_name)
    role = Role.objects.create(name=role_name)
    for slug in slugs:
        RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug=slug))
    GroupRole.objects.create(group=group, role=role)
    GroupAccess.objects.create(group=group, allowed_prefixes="\n".join(prefixes))
    return group


def make_reachable_but_uncapable_group(group_name, prefixes):
    """A Group with real GroupAccess reachability into the URL but NO
    Role/GroupRole/Capability at all -- the precise shape of the gap
    2.5E closes: before this phase, reaching the URL was the only
    boundary."""
    group = Group.objects.create(name=group_name)
    GroupAccess.objects.create(group=group, allowed_prefixes="\n".join(prefixes))
    return group


@override_settings(SECURE_SSL_REDIRECT=False)
class LibraryUploadAuthorizationTests(TestCase):
    def setUp(self):
        self.category = make_category()
        self.contributor = User.objects.create_user("contrib_2_5e", "contrib_2_5e@example.invalid", "pw")
        contrib_group, _ = Group.objects.get_or_create(name="Contributor")
        self.contributor.groups.add(contrib_group)
        # Contributor's category-pin rule matches on Category.code
        # case-insensitively against the username.
        self.contrib_category = Category.objects.create(
            code=self.contributor.username, name="Contributor Category", kind=self.category.kind,
        )

    def _upload(self, filename="Some Song.mp3", category_id=None):
        upload = SimpleUploadedFile(filename, b"not a real mp3, no tags readable", content_type="audio/mpeg")
        data = {"files": [upload]}
        if category_id is not None:
            data["category_id"] = category_id
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(LIBRARY_ROOT=tmp):
                return self.client.post(reverse("library:api-library-upload"), data)

    def test_unauthenticated_upload_rejected(self):
        resp = self._upload(category_id=self.category.id)
        self.assertIn(resp.status_code, (302, 401, 403))
        self.assertEqual(Track.objects.count(), 0)

    def test_reachable_user_without_library_upload_rejected(self):
        uncapable_group = make_reachable_but_uncapable_group(
            "2.5E Upload Reachable Only", ["/api/library/upload/"],
        )
        user = User.objects.create_user("uncapable_uploader", "u@example.invalid", "pw")
        user.groups.add(uncapable_group)
        self.client.force_login(user)
        resp = self._upload(category_id=self.category.id)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Track.objects.count(), 0)

    def test_contributor_with_library_upload_can_still_upload_to_own_category(self):
        self.client.force_login(self.contributor)
        resp = self._upload(filename="Contributor Upload.mp3")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["results"][0]["ok"], data["results"][0])
        track = Track.objects.get(id=data["results"][0]["track_id"])
        self.assertEqual(track.category_id, self.contrib_category.id)

    def test_contributor_cannot_smuggle_a_different_category(self):
        """The category-pin RESOURCE rule survives the new capability
        gate unchanged -- a Contributor posting someone else's
        category_id is still silently overridden to their own."""
        other_category = make_category(code="other")
        self.client.force_login(self.contributor)
        resp = self._upload(filename="Sneaky.mp3", category_id=other_category.id)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        track = Track.objects.get(id=data["results"][0]["track_id"])
        self.assertEqual(track.category_id, self.contrib_category.id)
        self.assertNotEqual(track.category_id, other_category.id)

    def test_superuser_can_upload_without_a_role(self):
        su = User.objects.create_superuser("upload_su_2_5e", "su@example.invalid", "pw")
        self.client.force_login(su)
        resp = self._upload(category_id=self.category.id)
        self.assertEqual(resp.status_code, 200)

    def test_staff_can_upload_without_a_role(self):
        staff = User.objects.create_user("upload_staff_2_5e", "staff@example.invalid", "pw", is_staff=True)
        self.client.force_login(staff)
        resp = self._upload(category_id=self.category.id)
        self.assertEqual(resp.status_code, 200)


@override_settings(SECURE_SSL_REDIRECT=False)
class TrackDetailAuthorizationTests(TestCase):
    def setUp(self):
        self.category = make_category()
        self.contributor = User.objects.create_user("td_contrib_2_5e", "td_contrib@example.invalid", "pw")
        contrib_group, _ = Group.objects.get_or_create(name="Contributor")
        self.contributor.groups.add(contrib_group)

        self.remote_dj = User.objects.create_user("td_dj_2_5e", "td_dj@example.invalid", "pw")
        dj_group, _ = Group.objects.get_or_create(name="remote_dj")
        self.remote_dj.groups.add(dj_group)

        self.own_unreviewed_track = make_track(
            "Own Unreviewed", category=self.category, uploaded_by=self.contributor, ready2air=False,
        )
        self.other_track = make_track("Someone Else's Track", category=self.category)

    def _detail_url(self, track):
        return reverse("library:api-track-detail", args=[track.pk])

    # ---- read is unchanged ----

    def test_read_is_unaffected_for_any_reachable_account(self):
        self.client.force_login(self.remote_dj)
        resp = self.client.get(self._detail_url(self.other_track))
        self.assertEqual(resp.status_code, 200)

    # ---- Contributor own-upload-delete carve-out preserved exactly ----

    def test_contributor_can_still_delete_own_unreviewed_upload(self):
        self.client.force_login(self.contributor)
        resp = self.client.delete(self._detail_url(self.own_unreviewed_track))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Track.objects.filter(pk=self.own_unreviewed_track.pk).exists())

    def test_contributor_still_cannot_delete_someone_elses_track(self):
        """Capability (library.upload) does not bypass the ownership
        RESOURCE rule -- Contributor holds library.upload but that
        alone never authorized deleting an arbitrary track."""
        self.client.force_login(self.contributor)
        resp = self.client.delete(self._detail_url(self.other_track))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Track.objects.filter(pk=self.other_track.pk).exists())

    def test_contributor_still_cannot_patch_even_their_own_upload(self):
        self.client.force_login(self.contributor)
        resp = self.client.patch(
            self._detail_url(self.own_unreviewed_track),
            data=json.dumps({"title": "Hijacked"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_remote_dj_remains_fully_read_only(self):
        self.client.force_login(self.remote_dj)
        resp = self.client.delete(self._detail_url(self.other_track))
        self.assertEqual(resp.status_code, 403)

    # ---- the actual 2.5E gap: the general (non-Contributor/remote_dj) branch ----

    def test_reachable_user_without_library_manage_tracks_rejected(self):
        uncapable_group = make_reachable_but_uncapable_group(
            "2.5E Track Reachable Only", ["/api/tracks/"],
        )
        user = User.objects.create_user("uncapable_track_editor", "u@example.invalid", "pw")
        user.groups.add(uncapable_group)
        self.client.force_login(user)

        resp = self.client.patch(
            self._detail_url(self.other_track),
            data=json.dumps({"title": "Hijacked"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        self.other_track.refresh_from_db()
        self.assertNotEqual(self.other_track.title, "Hijacked")

        resp = self.client.delete(self._detail_url(self.other_track))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Track.objects.filter(pk=self.other_track.pk).exists())

    def test_capable_manager_can_mutate_any_track_subject_to_no_ownership_rule(self):
        manager_group = make_capable_group(
            "2.5E Track Manager", "2.5E Track Manager Role", ["library.manage_tracks"], ["/api/tracks/"],
        )
        manager = User.objects.create_user("track_manager_2_5e", "m@example.invalid", "pw")
        manager.groups.add(manager_group)
        self.client.force_login(manager)

        resp = self.client.patch(
            self._detail_url(self.other_track),
            data=json.dumps({"title": "Renamed By Manager"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.other_track.refresh_from_db()
        self.assertEqual(self.other_track.title, "Renamed By Manager")

        resp = self.client.delete(self._detail_url(self.other_track))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Track.objects.filter(pk=self.other_track.pk).exists())

    def test_superuser_can_mutate_tracks_without_a_role(self):
        su = User.objects.create_superuser("track_su_2_5e", "su@example.invalid", "pw")
        self.client.force_login(su)
        resp = self.client.patch(
            self._detail_url(self.other_track),
            data=json.dumps({"title": "Renamed By Superuser"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_staff_can_mutate_tracks_without_a_role(self):
        staff = User.objects.create_user("track_staff_2_5e", "staff@example.invalid", "pw", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.patch(
            self._detail_url(self.other_track),
            data=json.dumps({"title": "Renamed By Staff"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)


@override_settings(SECURE_SSL_REDIRECT=False)
class TrackAutofillRelatedArtistsAuthorizationTests(TestCase):
    def setUp(self):
        self.category = make_category()
        self.track = make_track("Autofill Target", category=self.category)

        self.contributor = User.objects.create_user("af_contrib_2_5e", "af_contrib@example.invalid", "pw")
        contrib_group, _ = Group.objects.get_or_create(name="Contributor")
        self.contributor.groups.add(contrib_group)

        self.remote_dj = User.objects.create_user("af_dj_2_5e", "af_dj@example.invalid", "pw")
        dj_group, _ = Group.objects.get_or_create(name="remote_dj")
        self.remote_dj.groups.add(dj_group)

    def _autofill(self):
        return self.client.post(
            reverse("library:api-track-autofill-related-artists"),
            data=json.dumps({}),
            content_type="application/json",
        )

    def test_contributor_still_blocked_same_as_before(self):
        self.client.force_login(self.contributor)
        resp = self._autofill()
        self.assertEqual(resp.status_code, 403)

    def test_remote_dj_still_blocked_same_as_before(self):
        self.client.force_login(self.remote_dj)
        resp = self._autofill()
        self.assertEqual(resp.status_code, 403)

    def test_reachable_user_without_library_manage_tracks_rejected(self):
        """Proves the authority is now POSITIVE capability possession,
        not "not a member of the two named groups" -- a brand-new Group
        with real reachability and no capability is still denied."""
        uncapable_group = make_reachable_but_uncapable_group(
            "2.5E Autofill Reachable Only", ["/api/tracks/"],
        )
        user = User.objects.create_user("uncapable_autofill", "u@example.invalid", "pw")
        user.groups.add(uncapable_group)
        self.client.force_login(user)
        resp = self._autofill()
        self.assertEqual(resp.status_code, 403)

    def test_capable_user_allowed(self):
        manager_group = make_capable_group(
            "2.5E Autofill Manager", "2.5E Autofill Manager Role", ["library.manage_tracks"], ["/api/tracks/"],
        )
        manager = User.objects.create_user("autofill_manager_2_5e", "m@example.invalid", "pw")
        manager.groups.add(manager_group)
        self.client.force_login(manager)
        resp = self._autofill()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)["ok"])

    def test_superuser_allowed_without_a_role(self):
        su = User.objects.create_superuser("autofill_su_2_5e", "su@example.invalid", "pw")
        self.client.force_login(su)
        resp = self._autofill()
        self.assertEqual(resp.status_code, 200)

    def test_staff_allowed_without_a_role(self):
        staff = User.objects.create_user("autofill_staff_2_5e", "staff@example.invalid", "pw", is_staff=True)
        self.client.force_login(staff)
        resp = self._autofill()
        self.assertEqual(resp.status_code, 200)
