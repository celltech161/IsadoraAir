"""Hosted album-art base URL configurability (library/services/album_art.py
+ UITheme.hosted_album_art_base_url).

Covers: UITheme field validation, robust URL joining (_build_hosted_art_url),
the full resolver fallback chain with hosted art inserted at its documented
position, daily cache-busting, cache invalidation on a UITheme save that
changes/clears the base URL, and "oakgrove"/"hosted" backward compatibility
(see library/models.py's HOSTED_ART_SOURCES and album_art.py's own module
docstring for the full rationale -- Option C: new resolutions write
"hosted"; old "oakgrove" rows keep working with no data migration)."""
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from library.models import (
    Album, Artist, Category, CategoryKind,
    HOSTED_ART_INVALIDATION_SOURCES, HOSTED_ART_SOURCES,
    Track, UITheme,
)
from library.services.album_art import (
    _build_hosted_art_url,
    _hosted_art_lookup,
    resolve_album_art,
)


def make_track(artist_name="BirdNote", title="Test Track", music=True, **overrides):
    artist, _ = Artist.objects.get_or_create(name=artist_name)
    if music:
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        category, _ = Category.objects.get_or_create(code="TESTMUSIC", defaults={"name": "Test Music", "kind": kind})
    else:
        kind, _ = CategoryKind.objects.get_or_create(code="talk", defaults={"name": "Talk"})
        category, _ = Category.objects.get_or_create(code="TESTTALK", defaults={"name": "Test Talk", "kind": kind})
    defaults = dict(
        filepath=f"/nonexistent/{artist_name}-{title}.mp3",  # never a real file --
        # _extract_embedded_art_bytes fails soft (returns None, None) on any
        # file it can't open, so this naturally means "no embedded art"
        # without needing to mock _cache_embedded_art in every test.
        filename=f"{artist_name}-{title}.mp3",
        title=title,
        artist=artist,
        category=category,
    )
    defaults.update(overrides)
    return Track.objects.create(**defaults)


def head_response(status_code):
    resp = MagicMock()
    resp.status_code = status_code
    return resp


class HostedAlbumArtUrlValidationTests(TestCase):
    """UITheme.hosted_album_art_base_url: blank valid, http/https valid,
    trailing-slash/no-trailing-slash/subdirectory all valid, malformed and
    unsupported schemes rejected. Uses the field's own .clean() in
    isolation (not full_clean() on the whole model) so this can't be
    confused by unrelated fields."""

    def _clean(self, value):
        field = UITheme._meta.get_field("hosted_album_art_base_url")
        return field.clean(value, None)

    def test_blank_is_valid(self):
        self.assertEqual(self._clean(""), "")

    def test_https_valid(self):
        self.assertEqual(self._clean("https://artwork.example.org/"), "https://artwork.example.org/")

    def test_http_valid(self):
        self.assertEqual(self._clean("http://artwork.example.org/"), "http://artwork.example.org/")

    def test_trailing_slash_valid(self):
        self._clean("https://artwork.example.org/")  # no raise

    def test_no_trailing_slash_valid(self):
        self._clean("https://artwork.example.org")  # no raise

    def test_subdirectory_path_valid(self):
        self._clean("https://example.org/isadora/artwork/")  # no raise

    def test_malformed_url_rejected(self):
        with self.assertRaises(ValidationError):
            self._clean("not a url")

    def test_unsupported_scheme_rejected(self):
        with self.assertRaises(ValidationError):
            self._clean("ftp://artwork.example.org/")

    def test_javascript_scheme_rejected(self):
        with self.assertRaises(ValidationError):
            self._clean("javascript:alert(1)")


class HostedArtUrlBuildingTests(TestCase):
    """_build_hosted_art_url: robust joining, not fragile concatenation --
    the exact three cases called out in the roadmap spec."""

    def test_base_with_trailing_slash(self):
        url = _build_hosted_art_url("https://artwork.example.org/", "birdnote")
        self.assertEqual(url, "https://artwork.example.org/birdnote.png")

    def test_base_without_trailing_slash(self):
        url = _build_hosted_art_url("https://artwork.example.org", "birdnote")
        self.assertEqual(url, "https://artwork.example.org/birdnote.png")

    def test_base_with_subdirectory_path(self):
        url = _build_hosted_art_url("https://example.org/isadora/artwork/", "birdnote")
        self.assertEqual(url, "https://example.org/isadora/artwork/birdnote.png")

    def test_base_with_subdirectory_path_no_trailing_slash(self):
        # The classic urljoin footgun this helper specifically guards
        # against -- without the trailing-slash normalization, this
        # would drop "artwork" from the path entirely.
        url = _build_hosted_art_url("https://example.org/isadora/artwork", "birdnote")
        self.assertEqual(url, "https://example.org/isadora/artwork/birdnote.png")

    def test_blank_base_returns_none(self):
        self.assertIsNone(_build_hosted_art_url("", "birdnote"))


class HostedArtLookupUnitTests(TestCase):
    """_hosted_art_lookup in isolation, independent of the full resolver."""

    @patch("library.services.album_art.requests.head")
    def test_blank_base_url_performs_no_request(self, mock_head):
        result = _hosted_art_lookup("BirdNote", "")
        self.assertIsNone(result)
        mock_head.assert_not_called()

    @patch("library.services.album_art.requests.head")
    def test_hit_returns_url(self, mock_head):
        mock_head.return_value = head_response(200)
        result = _hosted_art_lookup("BirdNote", "https://artwork.example.org/")
        self.assertEqual(result, "https://artwork.example.org/birdnote.png")

    @patch("library.services.album_art.requests.head")
    def test_404_returns_none(self, mock_head):
        mock_head.return_value = head_response(404)
        self.assertIsNone(_hosted_art_lookup("BirdNote", "https://artwork.example.org/"))

    @patch("library.services.album_art.requests.head", side_effect=Exception("timeout"))
    def test_exception_returns_none(self, mock_head):
        self.assertIsNone(_hosted_art_lookup("BirdNote", "https://artwork.example.org/"))


class ResolverFallbackChainTests(TestCase):
    """resolve_album_art()'s full fallback chain with hosted art at its
    documented position: Album override > Artist override > cache >
    embedded > hosted > Deezer (music only) > iTunes (music only) > default."""

    def setUp(self):
        UITheme.objects.filter(pk=1).delete()

    def _set_hosted_base(self, url):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = url
        theme.save()

    @patch("library.services.album_art.requests.head")
    def test_hosted_hit_succeeds(self, mock_head):
        self._set_hosted_base("https://artwork.example.org/")
        mock_head.return_value = head_response(200)
        track = make_track(artist_name="BirdNote", music=False)

        result = resolve_album_art(track)

        self.assertEqual(result["source"], "hosted")
        self.assertTrue(result["art"].startswith("https://artwork.example.org/birdnote.png"))
        track.refresh_from_db()
        self.assertEqual(track.art_source, "hosted")

    @patch("library.services.album_art._itunes_search", return_value=(None, None))
    @patch("library.services.album_art._deezer_search", return_value=("https://deezer.example/art.jpg", "https://deezer.example/link"))
    @patch("library.services.album_art.requests.head")
    def test_hosted_404_falls_through_to_deezer(self, mock_head, mock_deezer, mock_itunes):
        self._set_hosted_base("https://artwork.example.org/")
        mock_head.return_value = head_response(404)
        track = make_track(artist_name="Some Band", music=True)

        result = resolve_album_art(track)

        self.assertEqual(result["source"], "deezer")
        mock_deezer.assert_called()

    @patch("library.services.album_art._itunes_search", return_value=(None, None))
    @patch("library.services.album_art._deezer_search", return_value=("https://deezer.example/art.jpg", None))
    @patch("library.services.album_art.requests.head", side_effect=Exception("dns error"))
    def test_hosted_exception_falls_through(self, mock_head, mock_deezer, mock_itunes):
        self._set_hosted_base("https://artwork.example.org/")
        track = make_track(artist_name="Some Band", music=True)

        result = resolve_album_art(track)

        self.assertEqual(result["source"], "deezer")

    @patch("library.services.album_art.requests.head")
    def test_blank_config_performs_no_hosted_request(self, mock_head):
        self._set_hosted_base("")
        track = make_track(artist_name="BirdNote", music=False)

        result = resolve_album_art(track)

        mock_head.assert_not_called()
        self.assertEqual(result["source"], "none")

    @patch("library.services.album_art.requests.head")
    def test_non_music_track_can_use_hosted_art(self, mock_head):
        self._set_hosted_base("https://artwork.example.org/")
        mock_head.return_value = head_response(200)
        track = make_track(artist_name="BirdNote", music=False)

        result = resolve_album_art(track)

        self.assertEqual(result["source"], "hosted")

    @patch("library.services.album_art._itunes_search", return_value=("https://itunes.example/art.jpg", None))
    @patch("library.services.album_art._deezer_search", return_value=(None, None))
    @patch("library.services.album_art.requests.head")
    def test_music_continues_to_deezer_then_itunes_after_hosted_miss(self, mock_head, mock_deezer, mock_itunes):
        self._set_hosted_base("https://artwork.example.org/")
        mock_head.return_value = head_response(404)
        track = make_track(artist_name="Some Band", music=True)

        result = resolve_album_art(track)

        mock_deezer.assert_called()
        mock_itunes.assert_called()
        self.assertEqual(result["source"], "itunes")

    @patch("library.services.album_art._cache_embedded_art", return_value="/media/album_art_cache/1.png")
    @patch("library.services.album_art.requests.head")
    def test_embedded_art_outranks_hosted(self, mock_head, mock_embedded):
        self._set_hosted_base("https://artwork.example.org/")
        track = make_track(artist_name="BirdNote", music=False)

        result = resolve_album_art(track)

        self.assertEqual(result["source"], "embedded")
        mock_head.assert_not_called()  # never reached hosted lookup at all

    @patch("library.services.album_art.requests.head")
    def test_artist_override_outranks_hosted(self, mock_head):
        self._set_hosted_base("https://artwork.example.org/")
        Artist.objects.create(name="Override Artist", cover_art="artist_covers/fake.jpg")
        # get_or_create inside make_track() finds this exact row by
        # name -- cover_art set above is preserved, not overwritten.
        track = make_track(artist_name="Override Artist", music=False)

        result = resolve_album_art(track)

        self.assertEqual(result["source"], "Artist Override")
        mock_head.assert_not_called()

    @patch("library.services.album_art.requests.head")
    def test_album_override_outranks_hosted(self, mock_head):
        self._set_hosted_base("https://artwork.example.org/")
        album = Album.objects.create(title="Override Album", cover_art="album_covers/fake.jpg")
        track = make_track(artist_name="BirdNote", music=False, album=album)

        result = resolve_album_art(track)

        self.assertEqual(result["source"], "Album Override")
        mock_head.assert_not_called()

    @patch("library.services.album_art._itunes_search", return_value=(None, None))
    @patch("library.services.album_art._deezer_search", return_value=(None, None))
    @patch("library.services.album_art.requests.head")
    def test_default_album_art_remains_final_fallback(self, mock_head, mock_deezer, mock_itunes):
        self._set_hosted_base("https://artwork.example.org/")
        mock_head.return_value = head_response(404)
        theme = UITheme.load()
        theme.default_album_art = "ui_theme/fake-default.jpg"
        theme.save()
        track = make_track(artist_name="Some Band", music=True)

        result = resolve_album_art(track)

        self.assertTrue(result["art"].endswith("fake-default.jpg"))


class DailyCacheBusterTests(TestCase):
    def setUp(self):
        UITheme.objects.filter(pk=1).delete()

    @patch("library.services.album_art.requests.head")
    def test_hosted_art_gets_date_cachebuster(self, mock_head):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://artwork.example.org/"
        theme.save()
        mock_head.return_value = head_response(200)
        track = make_track(artist_name="BirdNote", music=False)

        result = resolve_album_art(track)

        today = timezone.localdate().isoformat()
        self.assertEqual(result["art"], f"https://artwork.example.org/birdnote.png?v={today}")

    @patch("library.services.album_art._itunes_search", return_value=(None, None))
    @patch("library.services.album_art._deezer_search", return_value=("https://deezer.example/art.jpg", None))
    def test_deezer_art_has_no_cachebuster(self, mock_deezer, mock_itunes):
        track = make_track(artist_name="Some Band", music=True)
        result = resolve_album_art(track)
        self.assertEqual(result["art"], "https://deezer.example/art.jpg")
        self.assertNotIn("?v=", result["art"])

    @patch("library.services.album_art._cache_embedded_art", return_value="/media/album_art_cache/1.png")
    def test_embedded_art_has_no_cachebuster(self, mock_embedded):
        track = make_track(artist_name="BirdNote", music=False)
        result = resolve_album_art(track)
        self.assertEqual(result["art"], "/media/album_art_cache/1.png")


class CacheInvalidationTests(TestCase):
    """UITheme.save()'s invalidation of hosted-source Track cache rows
    when hosted_album_art_base_url changes (including to/from blank).
    Scoped strictly to hosted-origin rows -- everything else untouched."""

    def setUp(self):
        UITheme.objects.filter(pk=1).delete()
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://old.example.org/"
        theme.save()

        self.hosted_track = make_track(artist_name="Hosted Artist", music=False)
        self.hosted_track.art_url = "https://old.example.org/hosted-artist.png"
        self.hosted_track.art_source = "hosted"
        self.hosted_track.art_checked_at = timezone.now()
        self.hosted_track.save()

        self.legacy_oakgrove_track = make_track(artist_name="Legacy Artist", music=False)
        self.legacy_oakgrove_track.art_url = "https://old.example.org/legacy-artist.png"
        self.legacy_oakgrove_track.art_source = "oakgrove"
        self.legacy_oakgrove_track.art_checked_at = timezone.now()
        self.legacy_oakgrove_track.save()

        self.deezer_track = make_track(artist_name="Deezer Artist", title="Deezer Song", music=True)
        self.deezer_track.art_url = "https://deezer.example/art.jpg"
        self.deezer_track.art_source = "deezer"
        self.deezer_track.art_checked_at = timezone.now()
        self.deezer_track.save()

        self.itunes_track = make_track(artist_name="iTunes Artist", music=True)
        self.itunes_track.art_url = "https://itunes.example/art.jpg"
        self.itunes_track.art_source = "itunes"
        self.itunes_track.art_checked_at = timezone.now()
        self.itunes_track.save()

        self.embedded_track = make_track(artist_name="Embedded Artist", music=False)
        self.embedded_track.art_url = "/media/album_art_cache/embedded.png"
        self.embedded_track.art_source = "embedded"
        self.embedded_track.art_checked_at = timezone.now()
        self.embedded_track.save()

        self.none_track = make_track(artist_name="None Artist", music=False)
        self.none_track.art_url = ""
        self.none_track.art_source = "none"
        self.none_track.art_checked_at = timezone.now()
        self.none_track.save()

        self.play_count_marker = 7
        self.none_track.play_count = self.play_count_marker
        self.none_track.save()

    def test_changing_url_clears_hosted_and_oakgrove_rows(self):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://new.example.org/"
        theme.save()

        self.hosted_track.refresh_from_db()
        self.assertEqual(self.hosted_track.art_source, "")
        self.assertEqual(self.hosted_track.art_url, "")
        self.assertIsNone(self.hosted_track.art_checked_at)

        self.legacy_oakgrove_track.refresh_from_db()
        self.assertEqual(self.legacy_oakgrove_track.art_source, "")
        self.assertEqual(self.legacy_oakgrove_track.art_url, "")
        self.assertIsNone(self.legacy_oakgrove_track.art_checked_at)

    def test_deezer_cache_preserved(self):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://new.example.org/"
        theme.save()
        self.deezer_track.refresh_from_db()
        self.assertEqual(self.deezer_track.art_source, "deezer")
        self.assertEqual(self.deezer_track.art_url, "https://deezer.example/art.jpg")

    def test_itunes_cache_preserved(self):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://new.example.org/"
        theme.save()
        self.itunes_track.refresh_from_db()
        self.assertEqual(self.itunes_track.art_source, "itunes")

    def test_embedded_cache_preserved(self):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://new.example.org/"
        theme.save()
        self.embedded_track.refresh_from_db()
        self.assertEqual(self.embedded_track.art_source, "embedded")
        self.assertEqual(self.embedded_track.art_url, "/media/album_art_cache/embedded.png")

    def test_none_rows_ARE_invalidated_on_config_change(self):
        """Correction from an earlier draft of this feature: a "none"
        result is a conclusion drawn by exhausting the WHOLE chain
        (including hosted) under whatever hosted config was active AT
        THE TIME -- it is NOT evidence the row is independent of the
        hosted config the way an embedded/Deezer/iTunes hit is. Once
        the config changes (blank->URL, or URL A->URL B), that
        conclusion is stale and must be re-examined -- otherwise a
        track that was "none" only because hosted was disabled (or
        pointed at a different server) would be permanently stranded
        on default art even after an admin correctly configures
        hosted lookup. See HOSTED_ART_INVALIDATION_SOURCES."""
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://new.example.org/"
        theme.save()
        self.none_track.refresh_from_db()
        self.assertEqual(self.none_track.art_source, "")
        self.assertEqual(self.none_track.art_url, "")
        self.assertIsNone(self.none_track.art_checked_at)

    def test_unrelated_track_fields_unchanged(self):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://new.example.org/"
        theme.save()
        self.none_track.refresh_from_db()
        self.assertEqual(self.none_track.play_count, self.play_count_marker)
        self.hosted_track.refresh_from_db()
        self.assertEqual(self.hosted_track.title, "Test Track")

    def test_disabling_hosted_url_also_invalidates(self):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = ""
        theme.save()

        self.hosted_track.refresh_from_db()
        self.assertEqual(self.hosted_track.art_source, "")
        self.legacy_oakgrove_track.refresh_from_db()
        self.assertEqual(self.legacy_oakgrove_track.art_source, "")
        # "none" entries are swept up too when disabling -- harmless
        # (the next resolution just re-establishes "none" under the
        # now-disabled config, skipping the hosted step entirely) and
        # keeps the invalidation rule uniform rather than special-
        # cased per direction of the config change.
        self.none_track.refresh_from_db()
        self.assertEqual(self.none_track.art_source, "")

    @patch("library.services.album_art.requests.head")
    def test_none_cached_track_blank_to_url_discovers_hosted_art(self, mock_head):
        """The exact BirdNote scenario from review: a track cached as
        "none" while hosted lookup was disabled must NOT be
        permanently stuck on default art once an admin configures a
        real hosted base URL -- it gets a genuine chance to resolve
        against the newly-enabled source."""
        blank_theme = UITheme.load()
        blank_theme.hosted_album_art_base_url = ""
        blank_theme.save()
        birdnote = make_track(artist_name="BirdNote", music=False)
        birdnote.art_url = ""
        birdnote.art_source = "none"
        birdnote.art_checked_at = timezone.now()
        birdnote.save()

        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://artwork.oakgroveradio.com/"
        theme.save()

        birdnote.refresh_from_db()
        self.assertEqual(birdnote.art_source, "")  # invalidated

        mock_head.return_value = head_response(200)
        fresh = Track.objects.get(pk=birdnote.pk)
        result = resolve_album_art(fresh)

        self.assertEqual(result["source"], "hosted")
        self.assertTrue(result["art"].startswith("https://artwork.oakgroveradio.com/birdnote.png"))

    @patch("library.services.album_art.requests.head")
    def test_none_cached_track_url_a_to_url_b_can_resolve_against_b(self, mock_head):
        """Same scenario, repointing servers rather than enabling from
        blank: a track absent on server A (cached "none" while A was
        configured) might genuinely exist on server B -- it must be
        retried against B, not left stranded on A's (now-stale)
        negative result."""
        # setUp already configured "https://old.example.org/" (server A).
        artist_b = make_track(artist_name="Only On B", music=False)
        artist_b.art_url = ""
        artist_b.art_source = "none"
        artist_b.art_checked_at = timezone.now()
        artist_b.save()

        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://new.example.org/"  # server B
        theme.save()

        artist_b.refresh_from_db()
        self.assertEqual(artist_b.art_source, "")  # invalidated

        mock_head.return_value = head_response(200)
        fresh = Track.objects.get(pk=artist_b.pk)
        result = resolve_album_art(fresh)

        self.assertEqual(result["source"], "hosted")
        self.assertTrue(result["art"].startswith("https://new.example.org/only-on-b.png"))

    @patch("library.services.album_art.requests.head")
    def test_disabled_hosted_track_resolves_through_rest_of_chain(self, mock_head):
        """After disabling, the invalidated track's NEXT resolution
        should proceed embedded -> skip hosted -> Deezer/iTunes if
        music -> default, never re-serving the stale hosted URL."""
        theme = UITheme.load()
        theme.hosted_album_art_base_url = ""
        theme.save()

        # A fresh fetch, as any real request would do (e.g.
        # api_album_art's get_object_or_404) -- the invalidation is a
        # bulk .update(), which correctly never touches this
        # already-loaded in-memory instance's stale attributes.
        fresh = Track.objects.get(pk=self.hosted_track.pk)
        result = resolve_album_art(fresh)

        mock_head.assert_not_called()
        self.assertNotEqual(result["source"], "hosted")
        self.assertNotEqual(result.get("art"), "https://old.example.org/hosted-artist.png")

    def test_no_op_save_without_url_change_does_not_invalidate(self):
        """Saving UITheme for an unrelated reason (e.g. changing a
        color) must not touch hosted-art caches at all."""
        theme = UITheme.load()
        theme.accent = "#123456"
        theme.save()

        self.hosted_track.refresh_from_db()
        self.assertEqual(self.hosted_track.art_source, "hosted")

    def test_initial_creation_does_not_invalidate_anything(self):
        """UITheme.load()'s very first get_or_create() has no prior
        row to compare against -- must not explode or invalidate."""
        UITheme.objects.filter(pk=1).delete()
        UITheme.load()  # first-ever creation
        self.hosted_track.refresh_from_db()
        self.assertEqual(self.hosted_track.art_source, "hosted")


class BackwardCompatibilityTests(TestCase):
    """"oakgrove" is the historical persistent source token from before
    this setting was configurable; "hosted" is the new generic token.
    Both must be honored identically by every consumer."""

    def setUp(self):
        UITheme.objects.filter(pk=1).delete()

    def test_hosted_art_sources_constant_contains_both_tokens(self):
        self.assertEqual(HOSTED_ART_SOURCES, {"oakgrove", "hosted"})

    def test_invalidation_sources_is_strictly_broader_than_display_sources(self):
        self.assertEqual(HOSTED_ART_INVALIDATION_SOURCES, {"oakgrove", "hosted", "none"})
        self.assertTrue(HOSTED_ART_SOURCES.issubset(HOSTED_ART_INVALIDATION_SOURCES))
        # embedded/deezer/itunes must never be swept up by invalidation.
        self.assertFalse({"embedded", "deezer", "itunes"} & HOSTED_ART_INVALIDATION_SOURCES)

    def test_cached_oakgrove_row_returns_directly_without_new_lookup(self):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://artwork.example.org/"
        theme.save()
        track = make_track(artist_name="Legacy BirdNote", music=False)
        track.art_url = "https://artwork.example.org/legacy-birdnote.png"
        track.art_source = "oakgrove"
        track.art_checked_at = timezone.now()
        track.save()

        with patch("library.services.album_art.requests.head") as mock_head:
            result = resolve_album_art(track)
            mock_head.assert_not_called()  # cache hit, no re-lookup

        self.assertEqual(result["source"], "oakgrove")

    def test_cached_oakgrove_row_gets_cachebuster(self):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://artwork.example.org/"
        theme.save()
        track = make_track(artist_name="Legacy BirdNote", music=False)
        track.art_url = "https://artwork.example.org/legacy-birdnote.png"
        track.art_source = "oakgrove"
        track.art_checked_at = timezone.now()
        track.save()

        result = resolve_album_art(track)

        today = timezone.localdate().isoformat()
        self.assertEqual(result["art"], f"https://artwork.example.org/legacy-birdnote.png?v={today}")

    def test_new_resolutions_write_hosted_not_oakgrove(self):
        theme = UITheme.load()
        theme.hosted_album_art_base_url = "https://artwork.example.org/"
        theme.save()
        track = make_track(artist_name="Brand New Artist", music=False)

        with patch("library.services.album_art.requests.head") as mock_head:
            mock_head.return_value = head_response(200)
            resolve_album_art(track)

        track.refresh_from_db()
        self.assertEqual(track.art_source, "hosted")

    def test_oakgrove_still_a_valid_choice_for_admin_display(self):
        choice_values = dict(Track.ART_SOURCE_CHOICES)
        self.assertIn("oakgrove", choice_values)
        self.assertIn("hosted", choice_values)
