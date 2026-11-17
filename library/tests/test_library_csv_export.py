"""/library/ "Export to .csv": GET /api/tracks/export/.

Everything runs against the disposable test database; Track file paths are only
strings here (no audio, waveform or media file exists or is touched).

The file is parsed back with the standard csv module, so the assertions are about
what a spreadsheet/CSV reader sees, not about the writer's internals.
"""
import csv
import io
import json
import tempfile
from datetime import date, datetime, timezone as dt_timezone
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.db.models import QuerySet
from django.http import StreamingHttpResponse
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from library.models import Album, Artist, Category, CategoryKind, GroupAccess, Track
from library.services import track_csv_export as export
from library.views import TRACK_SORT_FIELDS

User = get_user_model()
URL = "library:api-track-export"
API = "library:api-track-list"
HEADER = ["Artist", "Title", "Album", "Category", "Duration"]


def read_csv(raw):
    """Parse raw response bytes like a spreadsheet would: BOM dropped, CRLF records, quoted newlines kept."""
    text = raw.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline="")))


class ExportBase(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self._n = 0
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.rock = Category.objects.create(code="ROCK", name="Rock", kind=kind)
        self.pop = Category.objects.create(code="POP", name="Pop", kind=kind)
        self.jazz = Category.objects.create(code="JAZZ", name="Jazz", kind=kind)
        self.admin = User.objects.create_superuser("csv-export-admin", "a@example.invalid", "pw")
        self.client.force_login(self.admin)

    def artist(self, name):
        return Artist.objects.get_or_create(name=name)[0]

    def album(self, title, artist):
        return Album.objects.get_or_create(title=title)[0]

    def track(self, title, artist="Alpha Band", category=None, album=None, duration=180.0, ready2air=True, **fields):
        self._n += 1
        artist_obj = self.artist(artist) if isinstance(artist, str) else artist
        album_obj = self.album(album, artist_obj) if isinstance(album, str) else album
        return Track.objects.create(
            filepath=str(self.root / f"t{self._n}.flac"), filename=f"t{self._n}.flac", title=title,
            artist=artist_obj, album=album_obj, category=category, format=fields.pop("format", "flac"),
            duration_seconds=duration, ready2air=ready2air, **fields)

    def download(self, **params):
        response = self.client.get(reverse(URL), {k: v for k, v in params.items() if v is not None})
        self.assertEqual(response.status_code, 200, getattr(response, "content", b"")[:200])
        raw = b"".join(response.streaming_content)
        return response, raw, read_csv(raw)

    def rows(self, **params):
        _response, _raw, table = self.download(**params)
        self.assertEqual(table[0], HEADER)
        return table[1:]

    def api_all(self, **params):
        """Every row the Library's own list endpoint returns for the same filters, paged to the end."""
        out, page = [], 1
        while True:
            body = self.client.get(reverse(API), {**{k: v for k, v in params.items() if v is not None},
                                                   "page": page, "per_page": 7}).json()
            out.extend(body["items"])
            if page >= body["pages"]:
                return out
            page += 1


@override_settings(SECURE_SSL_REDIRECT=False)
class FormatTests(ExportBase):
    def test_header_and_column_order_are_exact(self):
        self.track("Song", category=self.rock)
        response, raw, table = self.download()
        self.assertEqual(table[0], ["Artist", "Title", "Album", "Category", "Duration"])
        self.assertEqual(raw.decode("utf-8-sig").split("\r\n", 1)[0], "Artist,Title,Album,Category,Duration")
        self.assertTrue(all(len(row) == 5 for row in table))

    def test_content_type_attachment_and_streaming(self):
        self.track("Song")
        response = self.client.get(reverse(URL))
        self.assertIsInstance(response, StreamingHttpResponse)
        self.assertTrue(response.streaming)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertRegex(response["Content-Disposition"],
                         r'^attachment; filename="isadoraair-library-\d{4}-\d{2}-\d{2}\.csv"$')
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertFalse(response.has_header("Content-Length"))

    def test_file_starts_with_the_utf8_bom_and_the_header_is_still_exactly_artist(self):
        self.track("Song")
        _response, raw, table = self.download()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(table[0][0], "Artist")

    def test_one_row_per_track_with_the_five_values(self):
        self.track("Money", artist="Pink Floyd", album="Dark Side", category=self.rock, duration=382.4)
        self.assertEqual(self.rows(), [["Pink Floyd", "Money", "Dark Side", "ROCK", "6:22"]])

    def test_filename_uses_the_station_local_date_not_utc(self):
        self.track("Song")
        # 03:30 UTC on the 18th is still the evening of the 17th in Chicago.
        late_utc = datetime(2026, 11, 18, 3, 30, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=late_utc), \
                mock.patch("library.middleware.get_station_timezone", return_value="America/Chicago"):
            response = self.client.get(reverse(URL))
        self.assertEqual(response["Content-Disposition"],
                         'attachment; filename="isadoraair-library-2026-11-17.csv"')
        b"".join(response.streaming_content)
        with mock.patch("django.utils.timezone.now", return_value=late_utc), \
                mock.patch("library.middleware.get_station_timezone", return_value="Asia/Tokyo"):
            response = self.client.get(reverse(URL))
        self.assertEqual(response["Content-Disposition"],
                         'attachment; filename="isadoraair-library-2026-11-18.csv"')
        b"".join(response.streaming_content)


@override_settings(SECURE_SSL_REDIRECT=False)
class FilterTests(ExportBase):
    def setUp(self):
        super().setUp()
        self.a1 = self.track("Sunrise", "Alpha Band", self.rock, album="Dawn")
        self.a2 = self.track("Sunset", "Alpha Band", self.rock, ready2air=False)
        self.a3 = self.track("Moonlight", "Alpha Band", self.pop)
        self.b1 = self.track("Sunburst", "Beta Trio", self.pop, album="Dawn Again")
        self.g1 = self.track("Harbor", "Gamma Quartet", self.jazz)
        self.g2 = self.track("Hilltop", "Gamma Quartet", self.rock, related_artists="Beta Trio")
        self.d1 = self.track("Sundown", "Delta Solo", self.jazz, ready2air=False)
        self.a1.additional_categories.add(self.pop)     # primary ROCK + additional POP
        self.a3.additional_categories.add(self.pop)     # primary POP AND additional POP (join duplicates)
        self.g1.additional_categories.add(self.rock, self.pop)

    def titles(self, **params):
        return [row[1] for row in self.rows(**params)]

    def test_unfiltered_export_is_the_whole_library(self):
        self.assertEqual(sorted(self.titles()), sorted(t.title for t in Track.objects.all()))
        self.assertEqual(len(self.titles()), 7)

    def test_search_matches_title_artist_album_and_related_artists_like_the_library(self):
        self.assertEqual(self.titles(q="Sun", sort="title"), ["Sunburst", "Sundown", "Sunrise", "Sunset"])
        self.assertEqual(self.titles(q="Gamma", sort="title"), ["Harbor", "Hilltop"])
        self.assertEqual(self.titles(q="Dawn Again"), ["Sunburst"])
        self.assertEqual(self.titles(q="Beta Trio", sort="title"), ["Hilltop", "Sunburst"])   # related_artists + artist

    def test_plus_joins_required_terms(self):
        self.assertEqual(self.titles(q="Alpha + Sun", sort="title"), ["Sunrise", "Sunset"])
        self.assertEqual(self.titles(q="Alpha + zzz"), [])

    def test_search_category_and_status_combine(self):
        self.assertEqual(self.titles(q="Sun", category=self.pop.pk, ready2air="true", sort="title"),
                         ["Sunburst", "Sunrise"])
        self.assertEqual(self.titles(q="Sun", category=self.rock.pk, ready2air="false"), ["Sunset"])

    def test_category_matches_primary_and_additional_membership(self):
        # POP: primary (Moonlight, Sunburst) + additional (Sunrise via ROCK primary, Harbor via JAZZ primary).
        self.assertEqual(self.titles(category=self.pop.pk, sort="title"),
                         ["Harbor", "Moonlight", "Sunburst", "Sunrise"])
        self.assertEqual(self.titles(category=self.jazz.pk, sort="title"), ["Harbor", "Sundown"])
        self.assertEqual(self.titles(category=self.rock.pk, sort="title"),
                         ["Harbor", "Hilltop", "Sunrise", "Sunset"])

    def test_additional_category_matches_still_show_the_primary_category_code(self):
        by_title = {row[1]: row[3] for row in self.rows(category=self.pop.pk)}
        self.assertEqual(by_title, {"Harbor": "JAZZ", "Moonlight": "POP", "Sunburst": "POP", "Sunrise": "ROCK"})

    def test_each_track_exports_exactly_once_even_when_category_joins_duplicate_rows(self):
        # Moonlight: primary POP AND additional POP. Harbor: primary JAZZ, additional ROCK and POP.
        for category in (self.pop, self.rock, self.jazz):
            titles = self.titles(category=category.pk)
            self.assertEqual(len(titles), len(set(titles)), (category.code, titles))
            self.assertEqual(len(titles), self.client.get(reverse(API), {"category": category.pk}).json()["total"])

    def test_ready_to_air_filter(self):
        self.assertEqual(self.titles(ready2air="true", sort="title"),
                         ["Harbor", "Hilltop", "Moonlight", "Sunburst", "Sunrise"])
        self.assertEqual(self.titles(ready2air="false", sort="title"), ["Sundown", "Sunset"])
        self.assertEqual(len(self.titles(ready2air="bogus")), 7)       # unrecognised value = no filter, as in the API

    def test_empty_result_is_a_valid_header_only_csv(self):
        response, raw, table = self.download(q="zzz-no-such-thing")
        self.assertEqual(table, [HEADER])
        self.assertEqual(raw, b"\xef\xbb\xbfArtist,Title,Album,Category,Duration\r\n")
        self.assertEqual(response.status_code, 200)

    def test_empty_library_is_a_valid_header_only_csv(self):
        Track.objects.all().delete()
        self.assertEqual(self.download()[2], [HEADER])

    def test_a_non_numeric_category_is_a_client_error_not_a_500(self):
        response = self.client.get(reverse(URL), {"category": "abc"})
        self.assertEqual(response.status_code, 400)

    def test_export_matches_the_library_list_for_every_filter_and_sort_combination(self):
        """Drift guard: for each parameter set, the export holds exactly the Tracks the paged
        /api/tracks/ lists (all pages), in the same sort-key order."""
        fmt = export.format_duration
        for q in ("", "Sun", "Alpha + Sun", "Beta Trio", "zzz"):
            for category in ("", self.rock.pk, self.pop.pk, self.jazz.pk):
                for ready in ("", "true", "false"):
                    for sort in list(TRACK_SORT_FIELDS) + ["bogus"]:
                        for direction in ("asc", "desc"):
                            params = {"q": q or None, "category": category or None, "ready2air": ready or None,
                                      "sort": sort, "dir": direction}
                            expected = [(i["artist"], i["title"], i["album"], i["category_code"],
                                         fmt(i["duration_seconds"])) for i in self.api_all(**params)]
                            got = [tuple(r) for r in self.rows(**params)]
                            self.assertEqual(sorted(got), sorted(expected), params)
                            self.assertEqual(len(got), len(set(got)), params)       # all (artist,title) unique here
                            # Same sort KEY order (ties may differ in arbitrary order in the API itself).
                            key = {"title": lambda r: r[1], "artist": lambda r: r[0], "album": lambda r: r[2],
                                   "category": lambda r: r[3], "duration": lambda r: r[4]}.get(sort)
                            if key and sort in ("title", "artist", "album", "category"):
                                self.assertEqual([key(r) for r in got], [key(r) for r in expected], params)


@override_settings(SECURE_SSL_REDIRECT=False)
class PaginationAndSortTests(ExportBase):
    def test_every_match_is_exported_regardless_of_page_per_page_and_the_200_per_page_cap(self):
        # 260 > the list API's per_page cap of 200.
        for i in range(260):
            self.track(f"Song {i:03d}", category=self.rock)
        total = Track.objects.count()
        for extra in ({}, {"page": 2, "per_page": 5}, {"page": 99, "per_page": 1}, {"page": "x", "per_page": "y"}):
            self.assertEqual(len(self.rows(**extra)), total, extra)
        self.assertEqual(self.client.get(reverse(API), {"per_page": 500}).json()["total"], total)

    def test_bulk_selection_state_has_no_effect(self):
        for i in range(5):
            self.track(f"Song {i}")
        ids = list(Track.objects.values_list("pk", flat=True))
        self.assertEqual(len(self.rows(ids=",".join(map(str, ids[:2])), selected="1", excluded=str(ids[0]))), 5)

    def test_batching_does_not_drop_or_repeat_rows(self):
        for i in range(23):
            self.track(f"Song {i:02d}")
        with mock.patch.object(export, "BATCH_ROWS", 4):
            titles = [row[1] for row in self.rows()]
        self.assertEqual(titles, [f"Song {i:02d}" for i in range(23)])

    def test_sort_field_and_direction_are_respected(self):
        self.track("B Song", "Zed", self.jazz, "Aardvark", 200.0)
        self.track("A Song", "Yan", self.rock, "Cat", 100.0)
        self.track("C Song", "Xia", self.pop, "Bee", 300.0)
        order = lambda **p: [r[1] for r in self.rows(**p)]
        self.assertEqual(order(), ["A Song", "B Song", "C Song"])                         # default: title asc
        self.assertEqual(order(sort="title", dir="desc"), ["C Song", "B Song", "A Song"])
        self.assertEqual(order(sort="artist"), ["C Song", "A Song", "B Song"])           # Xia, Yan, Zed
        self.assertEqual(order(sort="artist", dir="desc"), ["B Song", "A Song", "C Song"])
        self.assertEqual(order(sort="album"), ["B Song", "C Song", "A Song"])            # Aardvark, Bee, Cat
        self.assertEqual(order(sort="category"), ["B Song", "C Song", "A Song"])         # JAZZ, POP, ROCK
        self.assertEqual(order(sort="duration"), ["A Song", "B Song", "C Song"])
        self.assertEqual(order(sort="duration", dir="desc"), ["C Song", "B Song", "A Song"])
        self.assertEqual(order(sort="nonsense", dir="sideways"), ["A Song", "B Song", "C Song"])   # API fallbacks

    def test_sort_on_a_tied_key_is_deterministic(self):
        for _ in range(6):
            self.track("Same", duration=100.0)
        first = list(Track.objects.order_by("pk").values_list("pk", flat=True))
        for _ in range(3):
            self.assertEqual(len(self.rows(sort="duration")), 6)
        self.assertEqual(first, sorted(first))

    def test_sort_respects_the_sort_after_filtering_with_category_joins(self):
        a = self.track("Zebra", category=self.rock)
        b = self.track("Apple", category=self.rock)
        c = self.track("Mango", category=self.jazz)
        for t in (a, b, c):
            t.additional_categories.add(self.pop, self.rock)
        self.assertEqual([r[1] for r in self.rows(category=self.pop.pk, sort="title")], ["Apple", "Mango", "Zebra"])
        self.assertEqual([r[1] for r in self.rows(category=self.pop.pk, sort="title", dir="desc")],
                         ["Zebra", "Mango", "Apple"])


@override_settings(SECURE_SSL_REDIRECT=False)
class RecordIdentityTests(ExportBase):
    def test_separate_tracks_with_identical_metadata_each_get_a_row(self):
        for _ in range(3):
            self.track("Echo", "Same Artist", self.rock, "Same Album", 200.0)
        rows = self.rows()
        self.assertEqual(rows, [["Same Artist", "Echo", "Same Album", "ROCK", "3:20"]] * 3)

    def test_identical_metadata_stays_separate_under_a_category_filter_and_search(self):
        for _ in range(3):
            t = self.track("Echo", "Same Artist", self.rock, "Same Album", 200.0)
            t.additional_categories.add(self.pop, self.jazz)
        self.assertEqual(len(self.rows(category=self.pop.pk)), 3)
        self.assertEqual(len(self.rows(category=self.rock.pk, q="Echo")), 3)
        self.assertEqual(len(self.rows(category=self.jazz.pk, sort="artist", dir="desc")), 3)


@override_settings(SECURE_SSL_REDIRECT=False)
class CellContentTests(ExportBase):
    def test_commas_quotes_and_embedded_line_breaks_round_trip(self):
        weird = {
            "title": 'Hello, "World"',
            "artist": "Last, First",
            "album": "Line one\nLine two\r\nLine three",
        }
        self.track(weird["title"], weird["artist"], self.rock, weird["album"])
        self.assertEqual(self.rows(), [[weird["artist"], weird["title"], weird["album"], "ROCK", "3:00"]])

    def test_the_raw_bytes_are_quoted_per_rfc_4180(self):
        self.track('He said "hi", twice', "A, B", self.rock, "x\ny")
        _response, raw, _table = self.download()
        self.assertIn(b'"A, B","He said ""hi"", twice","x\ny",ROCK,3:00\r\n', raw)

    def test_unicode_is_preserved(self):
        titles = ["Beyoncé – Déjà Vu", "東京ブギウギ", "Привет, мир", "naïve ☃ 🎵", "Ünïcödé", "İstanbul"]
        for i, title in enumerate(titles):
            self.track(title, f"Artïst {i} 🎸", self.rock, f"Álbum {i}")
        got = {row[1]: row for row in self.rows()}
        for i, title in enumerate(titles):
            self.assertEqual(got[title][:3], [f"Artïst {i} 🎸", title, f"Álbum {i}"])

    def test_missing_album_category_and_duration_are_blank(self):
        self.track("No Extras", category=None, album=None, duration=None)
        self.assertEqual(self.rows(), [["Alpha Band", "No Extras", "", "", ""]])

    def test_blank_cells_for_only_some_fields(self):
        self.track("A", category=self.rock, album=None, duration=61.0)
        self.track("B", category=None, album="Alb", duration=0.0)
        self.assertEqual(self.rows(sort="title"),
                         [["Alpha Band", "A", "", "ROCK", "1:01"], ["Alpha Band", "B", "Alb", "", "0:00"]])

    def test_a_track_whose_category_was_deleted_exports_a_blank_category(self):
        doomed = Category.objects.create(code="GONE", name="Gone", kind=self.rock.kind)
        self.track("Orphan", category=doomed)
        doomed.delete()
        self.assertEqual(self.rows(), [["Alpha Band", "Orphan", "", "", "3:00"]])


class DurationTests(TestCase):
    """The CSV Duration is the Library table's fmtDuration (library.html): round the whole value half-up,
    THEN split. The browser check also evaluates the page's own fmtDuration against these same inputs."""
    CASES = (
        (None, ""), (0, "0:00"), (0.4, "0:00"), (0.5, "0:01"), (1.5, "0:02"), (2.5, "0:03"),
        (59.0, "0:59"), (59.4, "0:59"), (59.49, "0:59"), (59.5, "1:00"), (59.6, "1:00"), (59.999, "1:00"),
        (60, "1:00"), (60.49, "1:00"), (60.5, "1:01"), (119.49, "1:59"), (119.5, "2:00"), (119.6, "2:00"),
        (179.5, "3:00"), (180, "3:00"), (3599.4, "59:59"), (3599.5, "60:00"), (3600, "60:00"), (36000, "600:00"),
        (float("nan"), ""), (float("inf"), ""),
    )

    def test_format_duration_cases(self):
        for seconds, expected in self.CASES:
            self.assertEqual(export.format_duration(seconds), expected, seconds)

    def test_never_renders_a_sixty_second_field(self):
        for tenth in range(0, 36000):
            text = export.format_duration(tenth / 10)
            minutes, secs = text.split(":")
            self.assertLess(int(secs), 60, text)
            self.assertEqual(len(secs), 2, text)

    def test_garbage_duration_is_blank(self):
        self.assertEqual(export.format_duration("abc"), "")
        self.assertEqual(export.format_duration(object()), "")


@override_settings(SECURE_SSL_REDIRECT=False)
class DurationInFileTests(ExportBase):
    def test_minute_boundaries_and_rounding_in_the_exported_file(self):
        cases = [(59.4, "0:59"), (59.5, "1:00"), (119.6, "2:00"), (119.49, "1:59"), (3599.5, "60:00"),
                 (0.0, "0:00"), (2.5, "0:03"), (None, "")]
        for i, (seconds, _expected) in enumerate(cases):
            self.track(f"T{i}", duration=seconds)
        got = {row[1]: row[4] for row in self.rows()}
        self.assertEqual(got, {f"T{i}": expected for i, (_s, expected) in enumerate(cases)})

    def test_file_duration_equals_the_library_page_rounding_for_the_same_tracks(self):
        for i, seconds in enumerate((59.5, 119.6, 61.2, 0.5, 3599.5)):
            self.track(f"T{i}", duration=seconds)
        listed = {item["title"]: item["duration_seconds"] for item in self.api_all()}
        got = {row[1]: row[4] for row in self.rows()}
        self.assertEqual(got, {title: export.format_duration(sec) for title, sec in listed.items()})


class FormulaGuardUnitTests(TestCase):
    def test_dangerous_leading_characters_are_neutralised(self):
        for value in ("=1+1", "+1", "-1", "@SUM(A1)", '=HYPERLINK("http://x","y")', "=cmd|' /C calc'!A0",
                      "-30-", "+44 20 7946", "@home", "==", "=", "+", "-", "@",
                      "\t=1", "\r=1", "\n=1", " =1+1", "  +1", "\u00a0=1", "\u2003@x",
                      "\tplain", "\rplain", "\nplain"):   # TAB/CR/LF-leading values too (OWASP)
            self.assertTrue(export.spreadsheet_safe(value).startswith("'"), repr(value))
            self.assertEqual(export.spreadsheet_safe(value)[1:], value)

    def test_ordinary_values_are_returned_untouched(self):
        for value in ("Money", "A=B", "Rock & Roll", "1+1=2", "Mr. -", "50 Cent", "(=)", "'quoted'", "Éclair",
                      "東京", "a@b.com", "x\t=1", "$100", "#1", "%", "<=>", "", "0"):
            self.assertEqual(export.spreadsheet_safe(value), value, repr(value))

    def test_none_is_blank(self):
        self.assertEqual(export.spreadsheet_safe(None), "")

    def test_guard_is_idempotent_only_in_the_sense_that_a_leading_apostrophe_is_not_a_trigger(self):
        once = export.spreadsheet_safe("=1")
        self.assertEqual(export.spreadsheet_safe(once), once)


@override_settings(SECURE_SSL_REDIRECT=False)
class FormulaGuardInFileTests(ExportBase):
    PAYLOADS = ('=1+1', '+SUM(1,2)', '-2+3', '@SUM(1)', '=HYPERLINK("http://evil.example","x")',
                "\t=1", "\r=1", " =1+1", "\tplain", "\rplain")

    def test_every_text_column_is_guarded(self):
        for i, payload in enumerate(self.PAYLOADS):
            cat = Category.objects.create(code=f"{payload}{i}"[:20], name=f"C{i}", kind=self.rock.kind)
            self.track(payload, payload, cat, payload)
        _response, _raw, table = self.download()
        self.assertEqual(len(table) - 1, len(self.PAYLOADS))
        for row in table[1:]:
            for cell in row[:4]:
                self.assertTrue(cell.startswith("'"), row)
                self.assertNotIn(cell[:1], ("=", "+", "-", "@", "\t", "\r"), row)
            self.assertRegex(row[4], r"^\d+:\d\d$")

    def test_the_original_value_is_recoverable_by_removing_the_one_apostrophe(self):
        self.track("=2+2", "+Artist", self.rock, "@Album")
        row = self.rows()[0]
        self.assertEqual([row[0][1:], row[1][1:], row[2][1:]], ["+Artist", "=2+2", "@Album"])

    def test_no_cell_anywhere_in_a_mixed_library_starts_with_a_formula_trigger(self):
        values = ["=a", "+b", "-c", "@d", "normal", "Éclair", " =x", "-", "50 Cent"]
        for i, v in enumerate(values):
            self.track(v, v, self.rock, v)
        for row in self.download()[2][1:]:
            for cell in row:
                self.assertNotIn(cell.lstrip()[:1], ("=", "+", "-", "@"), row)

    def test_safe_values_pass_through_verbatim(self):
        self.track("A=B", "Rock & Roll", self.rock, "1+1=2")
        self.assertEqual(self.rows(), [["Rock & Roll", "A=B", "1+1=2", "ROCK", "3:00"]])

    def test_filtering_and_sorting_still_use_the_real_values(self):
        self.track("-Dash", "Artist", self.rock)
        self.track("Plain", "Artist", self.rock)
        self.assertEqual([r[1] for r in self.rows(q="-Dash")], ["'-Dash"])


@override_settings(SECURE_SSL_REDIRECT=False)
class AccessTests(ExportBase):
    def setUp(self):
        super().setUp()
        self.track("Visible", category=self.rock)

    def fresh(self):
        self.client.logout()
        return self.client

    def test_anonymous_is_sent_to_login_and_receives_no_data(self):
        response = self.fresh().get(reverse(URL))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        self.assertFalse(getattr(response, "streaming", False))

    def test_authenticated_user_with_no_group_is_forbidden(self):
        self.client.force_login(User.objects.create_user("nogroup", "n@example.invalid", "pw"))
        response = self.client.get(reverse(URL))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b"Visible", response.content)
        self.assertEqual(self.client.get(reverse(API)).status_code, 403)       # same rule as the Library list

    def test_group_without_the_tracks_api_prefix_is_forbidden_like_the_list(self):
        group = Group.objects.create(name="Reports Only")
        GroupAccess.objects.create(group=group, allowed_prefixes="/reports/")
        user = User.objects.create_user("reportsonly", "r@example.invalid", "pw")
        user.groups.add(group)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse(URL)).status_code, 403)
        self.assertEqual(self.client.get(reverse(API)).status_code, 403)

    def test_an_exact_only_tracks_entry_does_not_unlock_the_export(self):
        group = Group.objects.create(name="Exact Only")
        GroupAccess.objects.create(group=group, allowed_exact="/api/tracks/")
        user = User.objects.create_user("exactonly", "e@example.invalid", "pw")
        user.groups.add(group)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse(API)).status_code, 200)
        self.assertEqual(self.client.get(reverse(URL)).status_code, 403)

    def test_library_read_only_groups_get_exactly_what_their_library_list_shows(self):
        for name in ("Contributor", "remote_dj"):
            user = User.objects.create_user(f"ro_{name}", f"{name}@example.invalid", "pw")
            user.groups.add(Group.objects.get_or_create(name=name)[0])
            self.client.force_login(user)
            listed = sorted(i["title"] for i in self.api_all())
            response = self.client.get(reverse(URL))
            self.assertEqual(response.status_code, 200, name)
            self.assertEqual(sorted(r[1] for r in read_csv(b"".join(response.streaming_content))[1:]), listed, name)
            self.assertEqual(listed, ["Visible"])

    def test_the_export_exposes_only_the_five_columns_never_paths_or_other_metadata(self):
        self.track("Secret path track", related_artists="Hidden Related", isrc="USABC1234567")
        _response, raw, table = self.download()
        text = raw.decode("utf-8")
        for leak in (str(self.root), "Hidden Related", "USABC1234567", ".flac", "filepath"):
            self.assertNotIn(leak, text)
        self.assertTrue(all(len(row) == 5 for row in table))

    def test_only_get_is_allowed(self):
        for method in ("post", "put", "patch", "delete"):
            response = getattr(self.client, method)(reverse(URL))
            self.assertEqual(response.status_code, 405, method)

    def test_the_export_is_read_only(self):
        for i in range(4):
            self.track(f"T{i}", category=self.rock).additional_categories.add(self.pop)
        before = list(Track.objects.order_by("pk").values())
        with CaptureQueriesContext(connection) as ctx:
            self.download(category=self.pop.pk)
        self.assertEqual(before, list(Track.objects.order_by("pk").values()))
        statements = [q["sql"].lstrip().split(None, 1)[0].upper() for q in ctx.captured_queries]
        self.assertTrue(statements and set(statements) <= {"SELECT", "SAVEPOINT", "RELEASE", "DECLARE", "FETCH", "CLOSE"}, statements)


@override_settings(SECURE_SSL_REDIRECT=False)
class StreamingAndQueryTests(ExportBase):
    def tracks(self, n):
        for i in range(n):
            t = self.track(f"Song {i:04d}", f"Artist {i % 9}", self.rock, f"Album {i % 5}", 100.0 + i)
            t.additional_categories.add(self.pop)

    def count_queries(self, **params):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse(URL), params)
            body = b"".join(response.streaming_content)
        return len(ctx.captured_queries), body, [q["sql"] for q in ctx.captured_queries]

    def test_query_count_is_constant_whatever_the_number_of_tracks(self):
        self.tracks(3)
        small, body_small, _ = self.count_queries(category=self.pop.pk)
        self.tracks(120)
        large, body_large, sqls = self.count_queries(category=self.pop.pk)
        self.assertEqual(large, small)
        self.assertGreater(len(body_large), len(body_small))
        self.assertEqual(sum('FROM "library_track"' in s for s in sqls), 1, sqls)    # one Track SELECT, no per-Track queries

    def test_no_per_track_queries_for_artist_album_or_category(self):
        self.tracks(40)
        _n, _body, sqls = self.count_queries()
        for table in ("library_artist", "library_album", "library_category"):
            self.assertEqual(sum(f'FROM "{table}"' in s for s in sqls), 0, (table, sqls))

    def test_the_body_is_streamed_in_bounded_batches_and_never_materialised(self):
        self.tracks(25)
        with mock.patch.object(export, "BATCH_ROWS", 4):
            response = self.client.get(reverse(URL))
            chunks = list(response.streaming_content)
        self.assertTrue(response.streaming)
        self.assertGreaterEqual(len(chunks), 1 + 25 // 4)
        self.assertEqual(chunks[0], b"\xef\xbb\xbfArtist,Title,Album,Category,Duration\r\n")   # header arrives alone, first
        self.assertTrue(all(chunk.count(b"\r\n") <= 4 for chunk in chunks[1:]))
        self.assertEqual(len(read_csv(b"".join(chunks))) - 1, 25)

    def test_rows_come_from_iterator_in_chunks_and_the_queryset_is_never_fully_fetched(self):
        self.tracks(30)
        real_iterator = QuerySet.iterator
        seen = {}

        def spy(qs, chunk_size=2000):
            seen["chunk_size"] = chunk_size
            return real_iterator(qs, chunk_size=chunk_size)

        real_fetch_all = QuerySet._fetch_all

        def forbidden(qs, *a, **k):
            if qs.model is Track:      # session / auth lookups legitimately use _fetch_all
                raise AssertionError("whole-queryset fetch (list()/len()/_fetch_all) of Tracks during export")
            return real_fetch_all(qs, *a, **k)

        with mock.patch.object(QuerySet, "iterator", spy), mock.patch.object(QuerySet, "_fetch_all", forbidden):
            response = self.client.get(reverse(URL), {"category": self.pop.pk})
            body = b"".join(response.streaming_content)
        self.assertEqual(seen["chunk_size"], export.ITERATOR_CHUNK_SIZE)
        self.assertEqual(len(read_csv(body)) - 1, 30)

    def test_a_small_iterator_chunk_size_still_exports_everything_once(self):
        self.tracks(37)
        with mock.patch.object(export, "ITERATOR_CHUNK_SIZE", 5), mock.patch.object(export, "BATCH_ROWS", 3):
            titles = [r[1] for r in self.rows(category=self.pop.pk)]
        self.assertEqual(titles, sorted(titles))
        self.assertEqual(len(titles), 37)
        self.assertEqual(len(set(titles)), 37)

    def test_no_temporary_file_is_created(self):
        self.tracks(5)
        boom = AssertionError("a temporary file was created for the export")
        with mock.patch("tempfile.NamedTemporaryFile", side_effect=boom), \
                mock.patch("tempfile.TemporaryFile", side_effect=boom), \
                mock.patch("tempfile.SpooledTemporaryFile", side_effect=boom), \
                mock.patch("tempfile.mkstemp", side_effect=boom), mock.patch("tempfile.mkdtemp", side_effect=boom):
            response = self.client.get(reverse(URL))
            self.assertEqual(len(read_csv(b"".join(response.streaming_content))) - 1, 5)
        self.assertNotIn("FileResponse", type(response).__name__)


@override_settings(SECURE_SSL_REDIRECT=False)
class ExistingLibraryBehaviourTests(ExportBase):
    """The list API (totals, artist_total, pagination) is unchanged by the export."""

    def test_list_totals_and_artist_total_are_untouched_and_match_the_export_row_count(self):
        for i in range(12):
            self.track(f"S{i}", f"Artist {i % 4}", self.rock)
        body = self.client.get(reverse(API), {"per_page": 5}).json()
        self.assertEqual((body["total"], body["artist_total"], body["pages"], len(body["items"])), (12, 4, 3, 5))
        self.assertEqual(set(body), {"items", "total", "artist_total", "page", "pages", "per_page"})
        self.assertEqual(len(self.rows()), body["total"])
        self.assertEqual(len({r[0] for r in self.rows()}), body["artist_total"])

    def test_the_export_does_not_shadow_a_track_detail_route(self):
        t = self.track("Detail")
        self.assertEqual(reverse("library:api-track-detail", args=[t.pk]), f"/api/tracks/{t.pk}/")
        self.assertEqual(reverse(URL), "/api/tracks/export/")
        self.assertEqual(self.client.get(f"/api/tracks/{t.pk}/").status_code, 200)


@override_settings(SECURE_SSL_REDIRECT=False)
class ToolbarRenderTests(ExportBase):
    def page(self, user=None):
        if user:
            self.client.force_login(user)
        response = self.client.get(reverse("library:library"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_staff_toolbar_has_the_export_button(self):
        html = self.page()
        self.assertIn('id="exportCsvBtn"', html)
        self.assertIn(">Export to .csv</button>", html)
        self.assertIn('onclick="exportCsv()"', html)

    def test_library_read_only_accounts_get_the_button_too(self):
        for name in ("Contributor", "remote_dj"):
            user = User.objects.create_user(f"ro_{name}", f"{name}@example.invalid", "pw")
            user.groups.add(Group.objects.get_or_create(name=name)[0])
            self.assertIn('id="exportCsvBtn"', self.page(user), name)

    def test_the_page_script_builds_the_export_url_from_current_controls_without_paging(self):
        html = self.page()
        start = html.index("function buildExportQuery()")
        body = html[start:html.index("function currentFilter()", start)]
        for needle in ("searchInput", "categoryFilter", "readyFilter", "currentSort", "currentDir",
                       "/api/tracks/export/?", "link.download", "link.click()"):
            self.assertIn(needle, body)
        for forbidden in ("currentPage", "per_page", "fetch(", "Blob", "selectedIds", "filterSelection"):
            self.assertNotIn(forbidden, body)
        self.assertEqual(self.client.get(reverse(URL)).status_code, 200)
