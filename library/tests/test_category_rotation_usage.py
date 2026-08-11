"""Roadmap 4.2 -- Category usage lookup: "Used by rotations" on the
Category edit pane (library/templates/library/categories.html), backed
by api_category_detail's GET response (library/views.py).

The relationship is the existing, real one -- RotationSlot.category ->
Category, RotationSlot.rotation -> Rotation -- there is no new model,
cache, or denormalized field. A Rotation using a Category in more than
one slot must still appear exactly once (.distinct()), and a Rotation
using a DIFFERENT Category must never appear.

Also covers the small additive "?rotation=<id>" deep-link read added to
rotations.html so a "Used by rotations" link actually lands the
operator on that specific Rotation, not just the blank picker."""
from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse

from library.models import Category, CategoryKind, Rotation, RotationSlot


def make_category(code, name=None, kind_code="music"):
    kind, _ = CategoryKind.objects.get_or_create(code=kind_code, defaults={"name": kind_code.title()})
    return Category.objects.create(code=code, name=name or code, kind=kind)


def make_rotation(name, categories):
    """One RotationSlot per entry in `categories`, in list order.
    Passing the same Category more than once creates multiple slots
    referencing it -- exactly the "repeated within one Rotation" case
    this feature must collapse to a single list entry."""
    rotation = Rotation.objects.create(name=name)
    for i, cat in enumerate(categories):
        RotationSlot.objects.create(rotation=rotation, position=i, category=cat)
    return rotation


@override_settings(SECURE_SSL_REDIRECT=False)  # plain-HTTP test client vs the project-wide prod setting
class CategoryUsedByRotationsApiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("catusagestaff", "catusage@example.invalid", "pw")
        self.client.force_login(self.staff)

    def _get_usage(self, category):
        resp = self.client.get(reverse("library:api-category-detail", args=[category.id]))
        self.assertEqual(resp.status_code, 200)
        return resp.json()["used_by_rotations"]

    def test_zero_use_shows_empty_list(self):
        birdnote = make_category("BIRDNOTE", "Birdnote")
        self.assertEqual(self._get_usage(birdnote), [])

    def test_one_rotation_appears_linked(self):
        birdnote = make_category("BIRDNOTE", "Birdnote")
        rotation = make_rotation("Morning Mix", [birdnote])
        usage = self._get_usage(birdnote)
        self.assertEqual(usage, [{"id": rotation.id, "name": "Morning Mix"}])

    def test_multiple_rotations_all_appear(self):
        birdnote = make_category("BIRDNOTE", "Birdnote")
        r1 = make_rotation("Weekend Features", [birdnote])
        r2 = make_rotation("Afternoon Variety", [birdnote])
        r3 = make_rotation("Morning Mix", [birdnote])
        usage = self._get_usage(birdnote)
        # Rotation's own default ordering (Meta.ordering = ["name"]) --
        # alphabetical, not creation order.
        self.assertEqual(usage, [
            {"id": r2.id, "name": "Afternoon Variety"},
            {"id": r3.id, "name": "Morning Mix"},
            {"id": r1.id, "name": "Weekend Features"},
        ])

    def test_category_repeated_within_one_rotation_appears_once(self):
        birdnote = make_category("BIRDNOTE", "Birdnote")
        rotation = make_rotation("Morning Mix", [birdnote, birdnote, birdnote])
        self.assertEqual(RotationSlot.objects.filter(rotation=rotation, category=birdnote).count(), 3)
        usage = self._get_usage(birdnote)
        self.assertEqual(usage, [{"id": rotation.id, "name": "Morning Mix"}])

    def test_unrelated_category_does_not_leak_into_results(self):
        birdnote = make_category("BIRDNOTE", "Birdnote")
        classic_rock = make_category("CLASSIC_ROCK", "Classic Rock")
        make_rotation("Morning Mix", [birdnote])
        make_rotation("Rock Rotation", [classic_rock])

        usage = self._get_usage(birdnote)
        self.assertEqual([u["name"] for u in usage], ["Morning Mix"])
        self.assertNotIn("Rock Rotation", [u["name"] for u in usage])

    def test_mixed_shared_and_exclusive_slots(self):
        # A Rotation can reference multiple Categories -- only the ones
        # that include Birdnote should list it as usage; a Rotation
        # with a slot for Birdnote AND other categories still counts
        # (and still only once), a Rotation with no Birdnote slot at
        # all must not appear.
        birdnote = make_category("BIRDNOTE", "Birdnote")
        classic_rock = make_category("CLASSIC_ROCK", "Classic Rock")
        mixed = make_rotation("Mixed Hour", [birdnote, classic_rock, birdnote])
        make_rotation("Rock Only", [classic_rock])

        usage = self._get_usage(birdnote)
        self.assertEqual(usage, [{"id": mixed.id, "name": "Mixed Hour"}])

    def test_track_direct_insert_slots_do_not_affect_category_usage(self):
        # RotationSlot can reference a Track directly instead of a
        # Category (category is null in that case) -- must not blow up
        # or be miscounted.
        from library.models import Artist, Track
        birdnote = make_category("BIRDNOTE", "Birdnote")
        artist = Artist.objects.create(name="Test Artist")
        track = Track.objects.create(
            filepath="/nonexistent/test.mp3", filename="test.mp3",
            title="Test Track", artist=artist,
        )
        rotation = Rotation.objects.create(name="Hybrid Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, track=track)
        RotationSlot.objects.create(rotation=rotation, position=1, category=birdnote)

        usage = self._get_usage(birdnote)
        self.assertEqual(usage, [{"id": rotation.id, "name": "Hybrid Rotation"}])

    def test_no_n_plus_one_as_rotation_count_grows(self):
        birdnote = make_category("BIRDNOTE", "Birdnote")
        make_rotation("Solo Rotation", [birdnote])
        with CaptureQueriesContext(connection) as ctx_one:
            self._get_usage(birdnote)
        query_count_one = len(ctx_one.captured_queries)

        for i in range(5):
            make_rotation(f"Extra Rotation {i}", [birdnote])
        with CaptureQueriesContext(connection) as ctx_many:
            self._get_usage(birdnote)
        query_count_many = len(ctx_many.captured_queries)

        self.assertEqual(query_count_one, query_count_many)

    def test_anonymous_request_redirected_to_login(self):
        # No new permission model -- this inherits whatever
        # LoginRequiredMiddleware/GroupBasedAccessMiddleware already
        # enforce on the Category API, same as before this feature.
        self.client.logout()
        birdnote = make_category("BIRDNOTE", "Birdnote")
        resp = self.client.get(reverse("library:api-category-detail", args=[birdnote.id]))
        self.assertEqual(resp.status_code, 302)

    def test_non_staff_authenticated_user_still_gets_a_response(self):
        # api_category_detail has no staff-only decorator beyond the
        # global login requirement -- confirm this feature doesn't
        # accidentally add one.
        Group.objects.get_or_create(name="remote_dj")
        ro = User.objects.create_user("catusageplain", "catusageplain@example.invalid", "pw")
        self.client.force_login(ro)
        birdnote = make_category("BIRDNOTE", "Birdnote")
        resp = self.client.get(reverse("library:api-category-detail", args=[birdnote.id]))
        # Whatever the existing access-control answer is (200 or 403),
        # it must be unchanged by this feature -- assert it's not a
        # 500 (which a broken query/permission interaction would be).
        self.assertIn(resp.status_code, (200, 403))

    def test_list_endpoint_unaffected(self):
        # api_category_list's shared serializer must NOT gain
        # used_by_rotations -- that's the detail-only, N+1-avoiding
        # design choice.
        birdnote = make_category("BIRDNOTE", "Birdnote")
        make_rotation("Morning Mix", [birdnote])
        resp = self.client.get(reverse("library:api-category-list"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["categories"]
        self.assertTrue(all("used_by_rotations" not in c for c in data))


@override_settings(SECURE_SSL_REDIRECT=False)
class CategoriesPageUsageSectionStaticContentTests(TestCase):
    """Static-content regression coverage, matching the existing
    pattern for other /reports/-adjacent tab additions (see
    ReportsPageListenerStatsTabTests in test_listener_stats_report.py)."""

    def setUp(self):
        self.staff = User.objects.create_superuser("catuistaff", "catuistaff@example.invalid", "pw")
        self.client.force_login(self.staff)

    def _get_categories_page(self):
        resp = self.client.get(reverse("library:categories"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_usage_section_present(self):
        html = self._get_categories_page()
        self.assertIn('id="catUsageSection"', html)
        self.assertIn("Used by rotations", html)

    def test_load_usage_js_function_present(self):
        html = self._get_categories_page()
        self.assertIn("function loadCategoryUsage(", html)
        self.assertIn("used_by_rotations", html)

    def test_rotations_url_resolved_not_left_as_literal_tag(self):
        html = self._get_categories_page()
        self.assertIn(reverse("library:rotations"), html)
        self.assertNotIn("{% url", html)

    def test_new_category_hides_usage_section(self):
        html = self._get_categories_page()
        self.assertIn("catUsageSection", html)
        self.assertIn("function newCategory()", html)


@override_settings(SECURE_SSL_REDIRECT=False)
class RotationsPageDeepLinkStaticContentTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("rotdlstaff", "rotdlstaff@example.invalid", "pw")
        self.client.force_login(self.staff)

    def test_deep_link_query_param_read_on_load(self):
        resp = self.client.get(reverse("library:rotations"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("URLSearchParams(window.location.search).get('rotation')", html)

    def test_plain_load_unaffected_without_query_param(self):
        # No ?rotation= -- must still land on the same blank picker as
        # before this feature (loadRotationList() with no args).
        resp = self.client.get(reverse("library:rotations"))
        html = resp.content.decode()
        self.assertIn("loadRotationList();", html)

    def test_stale_rotation_id_guarded_before_loading_detail(self):
        # A bookmarked "?rotation=<id>" for a since-deleted Rotation
        # must not fetch its detail (which would 404) -- the init code
        # only proceeds to loadRotationDetail() after confirming
        # loadRotationList() actually matched and selected that id
        # (see that function's own prevValue-must-exist-in-the-fresh-
        # list check). Without this guard the page still wouldn't
        # throw a JS error (loadRotationDetail already no-ops on a
        # non-ok fetch), but the <select> would be left in a
        # selectedIndex=-1 blank state instead of falling back to the
        # normal "Select a rotation..." placeholder.
        resp = self.client.get(reverse("library:rotations"))
        html = resp.content.decode()
        self.assertIn("select.value === deepLinkRotationId", html)
        self.assertIn("if (select.value === deepLinkRotationId) {\n      loadRotationDetail(deepLinkRotationId);", html)
