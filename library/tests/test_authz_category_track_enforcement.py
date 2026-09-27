"""Roadmap 2.5A security fixes + "reachability != authority" proofs.

Two concrete, previously-exploitable gaps closed here:

  1. library/views.py::api_category_detail (PATCH/DELETE) and
     api_category_list (POST) -- any Contributor account could mutate or
     delete ANY station category via their GroupAccess /api/categories/
     prefix (granted only so the upload page's category dropdown could
     read the list), with zero other check.
  2. A cluster of track-mutation endpoints discovered while closing (1)
     -- same shape, same /api/tracks/ prefix already granted to BOTH
     Contributor and remote_dj: bulk actions (including bulk delete),
     reanalyze, write-metadata, repick-cue-points, and the three
     blocked-slot toggle endpoints.

Every test in this file proves the central roadmap 2.5 property directly:
being able to reach a URL through GroupAccess must never, by itself,
grant authorization to perform the operation exposed there. Contributor
and remote_dj both KEEP their GroupAccess reachability into /api/tracks/
and /api/categories/ throughout (read paths continue to work) -- only the
specific mutating actions are newly denied.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability
from library.models import Artist, Category, CategoryKind, Track

User = get_user_model()


def make_category(code="rock", kind_code="music"):
    kind, _ = CategoryKind.objects.get_or_create(code=kind_code, defaults={"name": kind_code.title()})
    return Category.objects.create(code=code, name=code.title(), kind=kind)


def make_track(title="Test Track", category=None):
    artist, _ = Artist.get_or_create_ci("Authz Test Artist")
    return Track.objects.create(
        filepath=f"/srv/isadoraair/music/{title}.mp3",
        filename=f"{title}.mp3",
        format="mp3",
        title=title,
        artist=artist,
        category=category,
    )


@override_settings(SECURE_SSL_REDIRECT=False)
class LibraryManagementCapabilityTests(TestCase):
    def setUp(self):
        self.category = make_category()
        self.track = make_track(category=self.category)

        # A real Role bound to a real Group carrying BOTH the
        # capability (GroupRole) and the reachability (GroupAccess) a
        # legitimate library manager needs -- proves the "capable user
        # succeeds" side, not just the denial side.
        self.manager_group = Group.objects.create(name="Library Manager Test Group")
        role = Role.objects.create(name="Library Manager Test Role")
        for slug in ("library.manage_categories", "library.manage_tracks"):
            RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug=slug))
        GroupRole.objects.create(group=self.manager_group, role=role)
        from library.models import GroupAccess
        GroupAccess.objects.create(
            group=self.manager_group,
            allowed_prefixes="/api/categories/\n/api/tracks/\n/track/",
        )
        self.manager = User.objects.create_user("libmanager", "libmanager@example.invalid", "pw")
        self.manager.groups.add(self.manager_group)

        self.contributor = User.objects.create_user("contrib1", "contrib1@example.invalid", "pw")
        contrib_group, _ = Group.objects.get_or_create(name="Contributor")
        self.contributor.groups.add(contrib_group)

        self.remote_dj = User.objects.create_user("dj_authz_test", "dj_authz@example.invalid", "pw")
        dj_group, _ = Group.objects.get_or_create(name="remote_dj")
        self.remote_dj.groups.add(dj_group)

    # ---- category reads: must keep working for Contributor unchanged ----

    def test_contributor_can_still_read_category_list(self):
        self.client.force_login(self.contributor)
        resp = self.client.get(reverse("library:api-category-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(c["code"] == self.category.code for c in resp.json()["categories"]))

    def test_contributor_can_still_read_category_detail(self):
        self.client.force_login(self.contributor)
        resp = self.client.get(reverse("library:api-category-detail", args=[self.category.pk]))
        self.assertEqual(resp.status_code, 200)

    # ---- category writes: the fixed defect ----

    def test_contributor_cannot_create_category(self):
        self.client.force_login(self.contributor)
        resp = self.client.post(
            reverse("library:api-category-list"),
            data={"code": "hijacked", "name": "Hijacked", "kind_id": self.category.kind_id},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Category.objects.filter(code="hijacked").exists())

    def test_contributor_cannot_delete_category(self):
        self.client.force_login(self.contributor)
        resp = self.client.delete(reverse("library:api-category-detail", args=[self.category.pk]))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Category.objects.filter(pk=self.category.pk).exists())

    def test_contributor_cannot_patch_category(self):
        self.client.force_login(self.contributor)
        resp = self.client.patch(
            reverse("library:api-category-detail", args=[self.category.pk]),
            data={"name": "Renamed By Contributor"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        self.category.refresh_from_db()
        self.assertNotEqual(self.category.name, "Renamed By Contributor")

    def test_capable_manager_can_create_and_delete_category(self):
        self.client.force_login(self.manager)
        create_resp = self.client.post(
            reverse("library:api-category-list"),
            data={"code": "newcat", "name": "New Cat", "kind_id": self.category.kind_id},
            content_type="application/json",
        )
        self.assertEqual(create_resp.status_code, 200)
        new_id = create_resp.json()["id"]

        delete_resp = self.client.delete(reverse("library:api-category-detail", args=[new_id]))
        self.assertEqual(delete_resp.status_code, 200)
        self.assertFalse(Category.objects.filter(pk=new_id).exists())

    def test_superuser_can_mutate_categories_without_a_role(self):
        su = User.objects.create_superuser("catsu", "catsu@example.invalid", "pw")
        self.client.force_login(su)
        resp = self.client.patch(
            reverse("library:api-category-detail", args=[self.category.pk]),
            data={"name": "Renamed By Superuser"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)

    # ---- track mutation surface: the additional defect found while
    # ---- closing the category one, same reachability shape ----

    def test_contributor_cannot_bulk_mutate_tracks(self):
        self.client.force_login(self.contributor)
        resp = self.client.post(
            reverse("library:api-track-bulk"),
            data={"action": "delete", "ids": [self.track.pk]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Track.objects.filter(pk=self.track.pk).exists())

    def test_remote_dj_cannot_bulk_mutate_tracks(self):
        self.client.force_login(self.remote_dj)
        resp = self.client.post(
            reverse("library:api-track-bulk"),
            data={"action": "ready2air_on", "ids": [self.track.pk]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_remote_dj_cannot_toggle_blocked_slot(self):
        self.client.force_login(self.remote_dj)
        resp = self.client.post(
            reverse("library:api-track-blocked-slot-toggle", args=[self.track.pk]),
            data={"slot": 5},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        self.track.refresh_from_db()
        self.assertEqual(self.track.blocked_slots, [])

    def test_contributor_cannot_toggle_blocked_slot_row(self):
        self.client.force_login(self.contributor)
        resp = self.client.post(
            reverse("library:api-track-blocked-slot-toggle-row", args=[self.track.pk]),
            data={"day_of_week": 2},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_remote_dj_cannot_toggle_blocked_slot_column(self):
        self.client.force_login(self.remote_dj)
        resp = self.client.post(
            reverse("library:api-track-blocked-slot-toggle-column", args=[self.track.pk]),
            data={"hour": 3},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_contributor_cannot_write_metadata(self):
        self.client.force_login(self.contributor)
        resp = self.client.post(
            reverse("library:api-track-write-metadata", args=[self.track.pk]),
            data={"title": "Hijacked Title"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_capable_manager_can_toggle_blocked_slot(self):
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse("library:api-track-blocked-slot-toggle", args=[self.track.pk]),
            data={"slot": 5},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.track.refresh_from_db()
        self.assertEqual(self.track.blocked_slots, [5])

    def test_superuser_can_bulk_mutate_tracks_without_a_role(self):
        su = User.objects.create_superuser("tracksu", "tracksu@example.invalid", "pw")
        self.client.force_login(su)
        resp = self.client.post(
            reverse("library:api-track-bulk"),
            data={"action": "ready2air_on", "ids": [self.track.pk]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_staff_can_bulk_mutate_tracks_without_a_role(self):
        staff = User.objects.create_user("trackstaff", "trackstaff@example.invalid", "pw", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.post(
            reverse("library:api-track-bulk"),
            data={"action": "ready2air_on", "ids": [self.track.pk]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)

    # ---- reachability != authority, stated as directly as possible ----

    def test_contributor_group_access_reaches_the_url_but_authz_still_denies(self):
        """Direct proof of the central 2.5 property: Contributor's
        GroupAccess genuinely reaches /api/tracks/<pk>/blocked-slots/...
        (GroupBasedAccessMiddleware lets the request through -- no
        redirect, no middleware-level 403) yet the operation itself is
        still denied by authorize()."""
        self.client.force_login(self.contributor)
        resp = self.client.post(
            reverse("library:api-track-blocked-slot-toggle", args=[self.track.pk]),
            data={"slot": 1},
            content_type="application/json",
        )
        # 403 from the VIEW's own authorize() call, not a redirect/404
        # from the middleware -- proves the request reached the view.
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"], "Account lacks the 'library.manage_tracks' capability.")
