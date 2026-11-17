"""/library/: the unique-Artist count shown beside the Track count.

The count comes from the SAME filtered queryset as the Track total (one
``filter_tracks`` path -- search, Category, status), counts distinct Artist
records across every matching Track (not one page), and is one cheap query.
Everything runs against the disposable test database; no audio or waveform file
exists or is touched (Track file paths are only strings here).
"""
import json
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from library.models import Artist, Category, CategoryKind, Track
from library.services.track_filters import count_distinct_artists, filter_tracks

User = get_user_model()
API = "library:api-track-list"


@override_settings(SECURE_SSL_REDIRECT=False)
class UniqueArtistCountTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.rock = Category.objects.create(code="ROCK", name="Rock", kind=kind)
        self.pop = Category.objects.create(code="POP", name="Pop", kind=kind)
        self.jazz = Category.objects.create(code="JAZZ", name="Jazz", kind=kind)
        self.alpha = Artist.objects.create(name="Alpha Band")
        self.beta = Artist.objects.create(name="Beta Trio")
        self.gamma = Artist.objects.create(name="Gamma Quartet")
        self.delta = Artist.objects.create(name="Delta Solo")
        self.unused = Artist.objects.create(name="Artist With No Tracks")
        # Alpha x3 (one with an additional POP membership), Beta x1, Gamma x2, Delta x1 (not ready).
        self.a1 = self.track("Sunrise", self.alpha, self.rock)
        self.a2 = self.track("Sunset", self.alpha, self.rock, ready2air=False)
        self.a3 = self.track("Moonlight", self.alpha, self.pop)
        self.b1 = self.track("Sunburst", self.beta, self.pop)
        self.g1 = self.track("Harbor", self.gamma, self.jazz)
        self.g2 = self.track("Hilltop", self.gamma, self.rock, related_artists="Beta Trio")
        self.d1 = self.track("Sundown", self.delta, self.jazz, ready2air=False)
        # Additional-category membership: the category filter then JOINs, which
        # duplicates rows before DISTINCT collapses them.
        self.a1.additional_categories.add(self.pop)
        self.a3.additional_categories.add(self.pop)  # primary POP AND additional POP
        self.admin = User.objects.create_superuser("artist-count-admin", "a@example.invalid", "pw")
        self.client.force_login(self.admin)

    def track(self, title, artist, category, ready2air=True, **fields):
        return Track.objects.create(
            filepath=str(self.root / category.code / f"{title}.flac"), filename=f"{title}.flac",
            title=title, artist=artist, category=category, format="flac", duration_seconds=180.0,
            ready2air=ready2air, **fields)

    def api(self, **params):
        response = self.client.get(reverse(API), {k: v for k, v in params.items() if v is not None})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def expected(self, **params):
        """The total and distinct-artist count computed independently, from
        the actual matching Tracks the API itself lists (all pages)."""
        body = self.api(per_page=200, **params)
        artists = {item["artist"] for item in body["items"]}
        return body["total"], len(artists)

    # --------------------------------------------------------------- basics
    def test_unfiltered_counts_distinct_artists_not_tracks(self):
        body = self.api()
        self.assertEqual(body["total"], 7)
        self.assertEqual(body["artist_total"], 4)  # Alpha, Beta, Gamma, Delta; not the artist with no Tracks

    def test_multiple_tracks_by_the_same_artist_count_once(self):
        body = self.api(q="Alpha Band")
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["artist_total"], 1)

    def test_artist_with_no_tracks_is_never_counted(self):
        self.assertEqual(self.api(q="No Tracks")["artist_total"], 0)

    def test_artists_with_the_same_name_text_are_distinct_records_only_if_distinct_rows(self):
        # Artist.name is unique, so distinct records == distinct names; the
        # count is by record id.
        self.assertEqual(Artist.objects.filter(tracks__isnull=False).distinct().count(), 4)

    def test_empty_result_is_zero_and_zero(self):
        body = self.api(q="zzz-no-such-thing")
        self.assertEqual((body["total"], body["artist_total"]), (0, 0))

    # -------------------------------------------------------------- search
    def test_search_matches_exactly_the_tracks_the_track_count_matches(self):
        # title match ("Sun…": Sunrise, Sunset, Sunburst, Sundown -> Alpha, Beta, Delta)
        body = self.api(q="Sun")
        self.assertEqual((body["total"], body["artist_total"]), (4, 3))
        # artist-name match
        body = self.api(q="Gamma")
        self.assertEqual((body["total"], body["artist_total"]), (2, 1))
        # related_artists match: Hilltop is Gamma's Track that names Beta in related_artists;
        # Beta's own Track also matches -> two Tracks, two distinct primary artists.
        body = self.api(q="Beta Trio")
        self.assertEqual((body["total"], body["artist_total"]), (2, 2))

    def test_multi_term_plus_search_uses_the_same_semantics(self):
        body = self.api(q="Sun + Alpha")
        self.assertEqual((body["total"], body["artist_total"]), (2, 1))
        body = self.api(q="Sun + Delta")
        self.assertEqual((body["total"], body["artist_total"]), (1, 1))

    # ------------------------------------------------- category and status
    def test_category_filter_matches_primary_and_additional_membership_without_inflating(self):
        body = self.api(category=self.pop.pk)
        # POP: a1 (additional), a3 (primary + additional), b1 -> 3 Tracks; Alpha + Beta.
        self.assertEqual((body["total"], body["artist_total"]), (3, 2))
        body = self.api(category=self.rock.pk)
        self.assertEqual((body["total"], body["artist_total"]), (3, 2))   # a1, a2, g2 -> Alpha, Gamma
        body = self.api(category=self.jazz.pk)
        self.assertEqual((body["total"], body["artist_total"]), (2, 2))   # g1, d1 -> Gamma, Delta

    def test_status_filter(self):
        body = self.api(ready2air="true")
        self.assertEqual((body["total"], body["artist_total"]), (5, 3))   # Delta has no ready Track
        body = self.api(ready2air="false")
        self.assertEqual((body["total"], body["artist_total"]), (2, 2))   # a2 (Alpha), d1 (Delta)

    def test_combined_search_category_and_status(self):
        for params in (
            {"q": "Sun", "category": self.pop.pk},
            {"q": "Sun", "ready2air": "true"},
            {"category": self.rock.pk, "ready2air": "false"},
            {"q": "Sun", "category": self.jazz.pk, "ready2air": "false"},
            {"q": "Sun + Alpha", "category": self.rock.pk, "ready2air": "true"},
            {"q": "Hilltop", "category": self.pop.pk},
        ):
            with self.subTest(params=params):
                body = self.api(**params)
                total, artists = self.expected(**params)
                self.assertEqual((body["total"], body["artist_total"]), (total, artists))
        self.assertEqual(self.api(category=self.rock.pk, ready2air="false")["artist_total"], 1)
        self.assertEqual(self.api(q="Sun", category=self.jazz.pk, ready2air="false")["artist_total"], 1)

    def test_artist_count_always_agrees_with_the_listed_tracks_under_every_filter(self):
        from itertools import product
        queries = (None, "Sun", "Sun + Alpha", "Beta Trio", "o")
        categories = (None, self.rock.pk, self.pop.pk, self.jazz.pk)
        statuses = (None, "true", "false")
        for q, cat, status in product(queries, categories, statuses):
            with self.subTest(q=q, category=cat, ready2air=status):
                params = {"q": q, "category": cat, "ready2air": status}
                body = self.api(**params)
                total, artists = self.expected(**params)
                self.assertEqual(body["total"], total)
                self.assertEqual(body["artist_total"], artists)

    # ------------------------------------------------ pagination / sorting
    def test_count_spans_all_matching_tracks_not_the_current_page(self):
        full = self.api()
        for page in (1, 2, 3, 4, 99):  # 99 clamps to the last page, as the Track list already does
            with self.subTest(page=page):
                body = self.api(per_page=2, page=page)
                self.assertEqual(body["total"], full["total"])
                self.assertEqual(body["artist_total"], full["artist_total"])
        page_one = self.api(per_page=2, page=1)
        self.assertEqual(len(page_one["items"]), 2)
        self.assertGreater(page_one["artist_total"], len({i["artist"] for i in page_one["items"]}) - 1)
        # A single page showing one artist still reports the full count.
        by_artist = self.api(per_page=1, page=1, sort="artist", dir="asc")
        self.assertEqual(len(by_artist["items"]), 1)
        self.assertEqual(by_artist["artist_total"], 4)

    def test_sort_direction_and_field_do_not_change_either_count(self):
        for sort in ("title", "artist", "album", "category", "duration", "ready2air", "format"):
            for direction in ("asc", "desc"):
                with self.subTest(sort=sort, dir=direction):
                    body = self.api(sort=sort, dir=direction, q="Sun")
                    self.assertEqual((body["total"], body["artist_total"]), (4, 3))

    def test_existing_response_contract_is_preserved(self):
        body = self.api(per_page=3, page=2, sort="title", dir="desc")
        self.assertEqual(set(body), {"items", "total", "artist_total", "page", "pages", "per_page"})
        self.assertEqual((body["total"], body["page"], body["pages"], body["per_page"]), (7, 2, 3, 3))
        self.assertEqual([i["title"] for i in body["items"]], ["Sunburst", "Moonlight", "Hilltop"])
        self.assertEqual(set(body["items"][0]), {
            "id", "title", "artist", "album", "category_code", "category_name", "duration_seconds",
            "next_start_seconds", "format", "ready2air"})

    def test_track_total_is_unchanged_by_the_new_count(self):
        for params in ({}, {"q": "Sun"}, {"category": self.pop.pk}, {"ready2air": "true"}):
            with self.subTest(params=params):
                expected = filter_tracks(
                    Track.objects.all(), q=params.get("q", ""), category_id=params.get("category"),
                    ready2air=(params.get("ready2air") == "true") if "ready2air" in params else None).count()
                self.assertEqual(self.api(**params)["total"], expected)

    # ------------------------------------------------ null artist / helper
    def test_helper_reuses_the_filtered_queryset_and_excludes_null_artist_references(self):
        # Track.artist is NOT NULL today, so a Track without an Artist cannot be
        # created; the exclusion is a guard that must stay in the SQL.
        qs = Track.objects.all()
        sql = str(qs.order_by().filter(artist__isnull=False).values("artist_id").distinct().query)
        self.assertIn("IS NOT NULL", sql)
        self.assertEqual(count_distinct_artists(Track.objects.none()), 0)
        self.assertEqual(count_distinct_artists(Track.objects.all()), 4)
        self.assertEqual(count_distinct_artists(filter_tracks(Track.objects.all(), q="Sun")), 3)

    def test_null_artist_guard_is_applied_to_the_executed_query(self):
        with CaptureQueriesContext(connection) as queries:
            count_distinct_artists(Track.objects.all())
        (sql,) = [q["sql"] for q in queries]
        self.assertIn("IS NOT NULL", sql)

    # ---------------------------------------------------------- efficiency
    def test_artist_count_is_one_distinct_count_query_with_no_join_or_materialising(self):
        # Category + status only: a search term on the Artist name legitimately
        # joins the Artist table for the filter itself.
        with CaptureQueriesContext(connection) as queries:
            self.api(category=self.pop.pk, ready2air="true")
        marker = 'SELECT DISTINCT "library_track"."artist_id" AS'
        counts = [q["sql"] for q in queries if marker in q["sql"]]
        self.assertEqual(len(counts), 1, [q["sql"] for q in queries])
        sql = counts[0]
        self.assertTrue(sql.startswith("SELECT COUNT(*)"), sql)  # counted in the database, not in Python
        self.assertNotIn('JOIN "library_artist"', sql)           # counts the FK column; no Artist join
        self.assertNotIn("ORDER BY", sql)                        # caller ordering discarded
        self.assertNotIn('"library_track"."title"', sql)         # selects one column, not whole Tracks

    def test_query_count_does_not_grow_with_the_number_of_tracks_or_artists(self):
        def count_queries():
            with CaptureQueriesContext(connection) as queries:
                self.api(per_page=200)
            return len(queries)
        before = count_queries()
        for i in range(40):
            artist = Artist.objects.create(name=f"Bulk Artist {i}")
            self.track(f"Bulk {i}", artist, self.rock)
        self.assertEqual(count_queries(), before)
        self.assertEqual(self.api()["artist_total"], 44)

    # ----------------------------------------------------------------- page
    def test_library_page_renders_both_counts_from_one_response(self):
        html = self.client.get(reverse("library:library")).content.decode()
        self.assertIn("function libraryCountsLabel(total, artistTotal)", html)
        self.assertIn("libraryCountsLabel(data.total, data.artist_total)", html)
        # The old Track-only assignment is gone: the label can no longer be updated without the Artist count.
        self.assertNotIn("stats.textContent = data.total.toLocaleString() + ' tracks';", html)
