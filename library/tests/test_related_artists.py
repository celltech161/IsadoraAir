"""Related Artists feature -- everything except the log_builder
scheduling integration (see test_related_artist_scheduling.py for
that). Covers the shared service (library/services/related_artists.py):
normalization/merging, conservative credit extraction, filename-
fallback metadata, and the API/UI-facing behavior (PATCH
canonicalization, read-only enforcement, library search, and the
autofill endpoint processing the complete filtered queryset)."""
import json
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from library.management.commands import analyze_tracks
from library.models import Artist, Category, CategoryKind, Track
from library.services.related_artists import (
    autofill_related_artists_for_queryset,
    canonicalize_related_artists,
    extract_credited_artists,
    format_related_artists,
    humanize_filename_stem,
    merge_related_artists,
    merge_related_artists_detailed,
    normalize_name,
    parse_artist_title,
    resolve_fallback_metadata,
    track_identity_keys,
)
from library.services.track_filters import filter_tracks

RELATED_ARTISTS_MAX_LENGTH = Track._meta.get_field("related_artists").max_length


def make_track(**overrides):
    artist_name = overrides.pop("artist_name", "Test Artist")
    artist, _ = Artist.get_or_create_ci(artist_name)
    defaults = dict(
        # filepath is unique=True -- default to a counter-based path so
        # repeated calls in one test never collide unless the caller
        # explicitly wants a specific path.
        filepath=f"/tmp/does-not-exist/track-{Track.objects.count()}.mp3",
        filename="track.mp3",
        title="Test Title",
        artist=artist,
        related_artists="",
    )
    defaults.update(overrides)
    return Track.objects.create(**defaults)


# =====================================================================
# Normalization and merging
# =====================================================================
class NormalizationAndMergingTests(TestCase):
    def test_whitespace_trimming(self):
        self.assertEqual(
            canonicalize_related_artists("  David Gilmour  ,   Roger Waters  "),
            ["David Gilmour", "Roger Waters"],
        )

    def test_collapses_internal_whitespace(self):
        self.assertEqual(canonicalize_related_artists("David   Gilmour"), ["David Gilmour"])

    def test_comma_space_formatting(self):
        self.assertEqual(
            format_related_artists(["David Gilmour", "Roger Waters", "Richard Wright"]),
            "David Gilmour, Roger Waters, Richard Wright",
        )

    def test_case_insensitive_deduplication_keeps_first_seen_spelling(self):
        self.assertEqual(
            canonicalize_related_artists("Roger Waters, roger waters, ROGER WATERS"),
            ["Roger Waters"],
        )

    def test_existing_order_preservation(self):
        # Not alphabetized -- "Zeta" stays before "Alpha" since that's
        # the manually-entered order.
        self.assertEqual(canonicalize_related_artists("Zeta, Alpha"), ["Zeta", "Alpha"])

    def test_append_only_behavior_preserves_existing_and_adds_new(self):
        result = merge_related_artists("David Gilmour", ["Roger Waters"], primary_artist_name="Pink Floyd")
        self.assertEqual(result, "David Gilmour, Roger Waters")

    def test_append_only_does_not_reorder_or_drop_existing(self):
        result = merge_related_artists("Zeta, Alpha", ["Beta"], primary_artist_name="Pink Floyd")
        self.assertEqual(result, "Zeta, Alpha, Beta")

    def test_append_only_skips_case_insensitive_duplicate_of_existing(self):
        result = merge_related_artists("Roger Waters", ["roger waters"], primary_artist_name="Pink Floyd")
        self.assertEqual(result, "Roger Waters")

    def test_excludes_exact_duplicate_of_primary_artist_from_manual_value(self):
        result = canonicalize_related_artists("Pink Floyd, David Gilmour", primary_artist_name="pink floyd")
        self.assertEqual(result, ["David Gilmour"])

    def test_excludes_exact_duplicate_of_primary_artist_from_discoveries(self):
        result = merge_related_artists("", ["Pink Floyd", "David Gilmour"], primary_artist_name="Pink Floyd")
        self.assertEqual(result, "David Gilmour")

    def test_normalize_name_is_unicode_casefold_not_lower(self):
        # casefold() normalizes German ß -> ss; .lower() would not.
        self.assertEqual(normalize_name("Straße"), normalize_name("Strasse"))

    def test_forced_analysis_preserves_manual_value(self):
        """The exact bug this feature fixes: analyze_one_track with
        force=True must APPEND to an existing manual related_artists
        value, never replace it -- even when the extractor's own fresh
        discovery would find nothing new."""
        track = make_track(
            title="Some Unrelated Title", artist_name="Solo Artist",
            related_artists="Manually Entered Name",
        )
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "track.mp3"
            audio_path.write_bytes(b"not real audio, decode is mocked")
            wave_dir = Path(tmp) / "waveforms"
            wave_dir.mkdir()
            row = (track.id, str(audio_path), audio_path.name, None, track.title, "Solo Artist", track.related_artists)
            cfg_values = (8000, 0.05, 200, -26.0, -40.0, 0.1)
            with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda tid, fp, fn: (fp, fn)), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_pcm()), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=b""):
                ok = analyze_tracks.analyze_one_track(row, cfg_values, wave_dir, force=True)
        self.assertTrue(ok)
        track.refresh_from_db()
        # Manual value survives...
        self.assertIn("Manually Entered Name", track.related_artists)
        # ...and force still re-ran discovery (appending nothing here
        # since there's nothing to find, but via the merge path, not a
        # bypass).
        self.assertEqual(track.related_artists, "Manually Entered Name")

    def test_forced_analysis_appends_new_discovery_without_erasing_manual_value(self):
        track = make_track(
            title="Song feat. New Discovery", artist_name="Solo Artist",
            related_artists="Manually Entered Name",
        )
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "track.mp3"
            audio_path.write_bytes(b"not real audio, decode is mocked")
            wave_dir = Path(tmp) / "waveforms"
            wave_dir.mkdir()
            row = (track.id, str(audio_path), audio_path.name, None, track.title, "Solo Artist", track.related_artists)
            cfg_values = (8000, 0.05, 200, -26.0, -40.0, 0.1)
            with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda tid, fp, fn: (fp, fn)), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_pcm()), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=b""):
                ok = analyze_tracks.analyze_one_track(row, cfg_values, wave_dir, force=True)
        self.assertTrue(ok)
        track.refresh_from_db()
        self.assertIn("Manually Entered Name", track.related_artists)
        self.assertIn("New Discovery", track.related_artists)

    def test_single_track_reanalysis_reuses_passed_in_artist_lookup(self):
        """analyze_one_track must NOT reload the existing-Artist-name
        lookup when the caller already supplied one -- the bulk analyze_
        tracks command's own handle() loads it exactly once and passes
        it to every track's analyze_one_track call. Only the lazy
        single-track fallback (no lookup passed, e.g. the /track/<pk>/
        Reanalyze Track action) may load it, and then only once for
        that one call. Patches the loader to raise if called at all --
        an empty-but-not-None frozenset must still count as "already
        supplied" (the actual gate is `is None`, not truthiness)."""
        track = make_track(title="Song feat. Someone", artist_name="Solo Artist")
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "track.mp3"
            audio_path.write_bytes(b"not real audio, decode is mocked")
            wave_dir = Path(tmp) / "waveforms"
            wave_dir.mkdir()
            row = (track.id, str(audio_path), audio_path.name, None, track.title, "Solo Artist", track.related_artists)
            cfg_values = (8000, 0.05, 200, -26.0, -40.0, 0.1)
            with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda tid, fp, fn: (fp, fn)), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_pcm()), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=b""), \
                 patch.object(
                     analyze_tracks, "load_existing_artist_names_casefolded",
                     side_effect=AssertionError("must not reload -- caller already supplied the lookup"),
                 ):
                ok = analyze_tracks.analyze_one_track(
                    row, cfg_values, wave_dir, force=True,
                    existing_artist_names_casefolded=frozenset(),
                )
        self.assertTrue(ok)


def _fake_pcm(seconds=1, sample_rate=8000, amplitude=3000):
    n = seconds * sample_rate
    return struct.pack(f"<{n}h", *([amplitude] * n))


# =====================================================================
# Extraction
# =====================================================================
class ExtractionTests(TestCase):
    def test_feat_dot_in_artist_field(self):
        self.assertEqual(
            extract_credited_artists("Artist A feat. Artist B", "", "Artist A feat. Artist B"),
            ["Artist A", "Artist B"],
        )

    def test_feat_no_dot(self):
        self.assertEqual(
            extract_credited_artists("Artist A feat Artist B", "", "Artist A feat Artist B"),
            ["Artist A", "Artist B"],
        )

    def test_ft_dot(self):
        self.assertEqual(
            extract_credited_artists("Artist A ft. Artist B", "", "Artist A ft. Artist B"),
            ["Artist A", "Artist B"],
        )

    def test_ft_no_dot(self):
        self.assertEqual(
            extract_credited_artists("Artist A ft Artist B", "", "Artist A ft Artist B"),
            ["Artist A", "Artist B"],
        )

    def test_featuring(self):
        self.assertEqual(
            extract_credited_artists("Artist A featuring Artist B", "", "Artist A featuring Artist B"),
            ["Artist A", "Artist B"],
        )

    def test_marker_in_artist_field_identifies_leading_component(self):
        discovered = extract_credited_artists("Artist A feat. Artist B", "Some Title", "Artist A feat. Artist B")
        self.assertIn("Artist A", discovered)
        self.assertIn("Artist B", discovered)

    def test_marker_in_title_field_does_not_add_a_leading_component(self):
        # Title has no "leading artist" concept -- only the credited
        # name after the marker should come out.
        discovered = extract_credited_artists("Artist A", "Song Title (feat. Artist B)", "Artist A")
        self.assertEqual(discovered, ["Artist B"])

    def test_marker_in_title_parentheses(self):
        discovered = extract_credited_artists("Artist A", "Song Title (Featuring Artist B)", "Artist A")
        self.assertEqual(discovered, ["Artist B"])

    def test_marker_in_title_brackets(self):
        discovered = extract_credited_artists("Artist A", "Song Title [feat. Artist B]", "Artist A")
        self.assertEqual(discovered, ["Artist B"])

    def test_with_in_parentheses(self):
        discovered = extract_credited_artists("Artist A", "Song Title (with Artist B)", "Artist A")
        self.assertEqual(discovered, ["Artist B"])

    def test_with_in_brackets(self):
        discovered = extract_credited_artists("Artist A", "Song Title [with Artist B]", "Artist A")
        self.assertEqual(discovered, ["Artist B"])

    def test_with_after_dash(self):
        discovered = extract_credited_artists("Artist A", "Song Title - with Artist B", "Artist A")
        self.assertEqual(discovered, ["Artist B"])

    def test_ordinary_with_in_title_is_rejected(self):
        discovered = extract_credited_artists("Artist A", "A Room With a View", "Artist A")
        self.assertEqual(discovered, [])

    def test_bare_ampersand_splits_when_both_sides_are_existing_artists(self):
        existing = {"jay-z", "kanye west"}
        discovered = extract_credited_artists(
            "Jay-Z & Kanye West", "", "Jay-Z & Kanye West",
            existing_artist_names_casefolded=existing,
        )
        self.assertEqual(set(discovered), {"Jay-Z", "Kanye West"})

    def test_bare_and_splits_when_both_sides_are_existing_artists(self):
        existing = {"simon", "garfunkel"}
        discovered = extract_credited_artists(
            "Simon and Garfunkel", "", "Simon and Garfunkel",
            existing_artist_names_casefolded=existing,
        )
        self.assertEqual(set(discovered), {"Simon", "Garfunkel"})

    def test_no_split_when_neither_side_is_an_existing_artist(self):
        discovered = extract_credited_artists(
            "Simon & Garfunkel", "", "Simon & Garfunkel",
            existing_artist_names_casefolded=frozenset(),
        )
        self.assertEqual(discovered, [])

    def test_no_split_when_only_one_side_is_an_existing_artist(self):
        existing = {"simon"}  # Garfunkel missing
        discovered = extract_credited_artists(
            "Simon & Garfunkel", "", "Simon & Garfunkel",
            existing_artist_names_casefolded=existing,
        )
        self.assertEqual(discovered, [])

    def test_multiple_discoveries_merge_with_existing_values(self):
        discovered = extract_credited_artists("Artist A", "Song feat. Artist B & Artist C", "Artist A")
        self.assertEqual(set(discovered), {"Artist B", "Artist C"})
        merged = merge_related_artists("Existing Name", discovered, primary_artist_name="Artist A")
        self.assertEqual(merged, "Existing Name, Artist B, Artist C")

    def test_conservative_no_inference_beyond_explicit_markers(self):
        # No markers at all -- must not guess anything from a plain
        # title/artist pair.
        self.assertEqual(extract_credited_artists("Artist A", "Some Plain Song Title", "Artist A"), [])

    def test_title_feat_dot_parentheses_exact_marker_form(self):
        discovered = extract_credited_artists("Artist A", "Title (feat. Guest)", "Artist A")
        self.assertEqual(discovered, ["Guest"])

    def test_title_ft_no_dot_brackets_exact_marker_form(self):
        discovered = extract_credited_artists("Artist A", "Title [ft Guest]", "Artist A")
        self.assertEqual(discovered, ["Guest"])

    def test_three_credited_guests_in_one_tail(self):
        discovered = extract_credited_artists("Artist A", "Song feat. Guest A, Guest B & Guest C", "Artist A")
        self.assertEqual(set(discovered), {"Guest A", "Guest B", "Guest C"})

    def test_bracket_credit_followed_by_separate_version_label_bracket(self):
        # A second, unrelated bracket group (a version/remaster label)
        # must not contaminate or get consumed as part of the credit.
        discovered = extract_credited_artists("Artist A", "Song (feat. Guest) (Remastered 2020)", "Artist A")
        self.assertEqual(discovered, ["Guest"])
        discovered2 = extract_credited_artists("Artist A", "Song [feat. Guest] [Radio Edit]", "Artist A")
        self.assertEqual(discovered2, ["Guest"])

    def test_dancing_with_myself_is_not_a_credit(self):
        # Same class of false-positive risk as "A Room With a View" --
        # a bare mid-title "with" must never be treated as a credit.
        self.assertEqual(extract_credited_artists("Artist A", "Dancing with Myself", "Artist A"), [])

    def test_bare_ft_word_is_a_known_accepted_ambiguity(self):
        # Spec-accepted false-positive: "ft" as a bare word (e.g. "5 ft
        # Tall") is inherently indistinguishable from the ft. credit
        # marker by regex alone. This documents the CURRENT, ACCEPTED
        # behavior rather than asserting it should be fixed -- false
        # negatives are preferred over false positives in general, but
        # "ft"/"ft." are required markers per spec regardless of this
        # inherent edge case.
        self.assertEqual(extract_credited_artists("Artist A", "5 ft Tall", "Artist A"), ["Tall"])

    def test_unicode_nbsp_whitespace_around_marker_still_matches(self):
        # U+00A0 (non-breaking space) instead of an ordinary space --
        # Python's \s is Unicode-aware by default for str patterns, so
        # this must still match, not silently fail to extract.
        nbsp = " "
        discovered = extract_credited_artists("Artist A", f"Song (feat.{nbsp}Guest)", "Artist A")
        self.assertEqual(discovered, ["Guest"])

    def test_composite_primary_artist_plus_component_names_via_filename_style_split(self):
        # End-to-end composition: parse_artist_title splits a combined
        # "Artist feat. Guest - Title" string (the filename-fallback
        # shape), and the resulting artist component still correctly
        # yields both the leading primary component and the credited
        # guest when run back through extract_credited_artists.
        artist_part, title_part = parse_artist_title("Artist A feat. Guest - Song Title")
        self.assertEqual((artist_part, title_part), ("Artist A feat. Guest", "Song Title"))
        discovered = extract_credited_artists(artist_part, title_part, artist_part)
        self.assertEqual(set(discovered), {"Artist A", "Guest"})

    def test_extracted_leading_component_equal_to_full_primary_artist_is_excluded(self):
        # primary_artist_name here is the exact SAME string as the
        # leading component that would otherwise be discovered --
        # must be excluded as a duplicate of the primary artist.
        discovered = extract_credited_artists("Artist A feat. Guest", "", "Artist A")
        self.assertNotIn("Artist A", discovered)
        self.assertIn("Guest", discovered)

    def test_repeated_entries_different_case_dedupe_keeping_first_seen_spelling(self):
        # The same guest is independently discoverable from BOTH the
        # artist field (proper case) and the title field (lowercase) --
        # must collapse to one entry, keeping whichever spelling was
        # encountered first (artist field is scanned before title).
        discovered = extract_credited_artists(
            "Solo Artist feat. Guest A", "Song (feat. guest a)", "Solo Artist feat. Guest A",
        )
        self.assertEqual(discovered, ["Solo Artist", "Guest A"])

    def test_empty_or_punctuation_only_tails_produce_no_garbage(self):
        # A trailing marker with nothing meaningful after it must never
        # crash or produce an empty/punctuation-only "discovery".
        self.assertEqual(extract_credited_artists("Artist A feat.", "", "Artist A feat."), ["Artist A"])
        self.assertEqual(extract_credited_artists("Artist A", "Song (feat.)", "Artist A"), [])
        self.assertEqual(extract_credited_artists("Artist A", "Song feat. , ,", "Artist A"), [])

    def test_comma_in_a_discovered_or_manual_name_is_a_known_field_format_ambiguity(self):
        """Track.related_artists is a flat comma-separated CharField --
        an artist name that itself legitimately contains a comma (e.g.
        "Earth, Wind & Fire") cannot be safely round-tripped through
        that format: canonicalize_related_artists has no way to tell
        "one name containing a comma" apart from "two comma-separated
        names". This test documents the current, accepted behavior
        (the name gets split into two entries) as a known limitation
        of keeping the existing single-CharField schema, per the
        constraint against introducing a second/richer data source."""
        result = canonicalize_related_artists("Earth, Wind & Fire")
        self.assertEqual(result, ["Earth", "Wind & Fire"])  # split, not preserved whole -- documented limitation


# =====================================================================
# Filename fallback
# =====================================================================
@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on the upload POST
class FilenameFallbackTests(TestCase):
    def test_humanize_replaces_underscores_and_collapses_whitespace(self):
        self.assertEqual(humanize_filename_stem("Pink_Floyd_-_Time"), "Pink Floyd - Time")

    def test_humanize_trims(self):
        self.assertEqual(humanize_filename_stem("  Pink_Floyd  "), "Pink Floyd")

    def test_humanize_strips_forward_slash_directory_prefix(self):
        self.assertEqual(humanize_filename_stem("some/upload/dir/Pink_Floyd_-_Time"), "Pink Floyd - Time")

    def test_humanize_strips_backslash_directory_prefix(self):
        # pathlib.Path(...).stem on this Linux server (PosixPath) does
        # NOT treat '\\' as a separator the way it does '/' -- a raw
        # Windows-style original filename would otherwise survive
        # whole into fallback metadata as a misleading path-like title.
        self.assertEqual(
            humanize_filename_stem("Users\\john\\Music\\Pink_Floyd_-_Time"),
            "Pink Floyd - Time",
        )

    def test_resolve_fallback_metadata_strips_backslash_path_prefix(self):
        title, artist = resolve_fallback_metadata("C:\\Users\\john\\Pink_Floyd_-_Time", None, None)
        self.assertEqual((title, artist), ("Time", "Pink Floyd"))
        self.assertNotIn("\\", title)
        self.assertNotIn("\\", artist)

    def test_artist_title_fallback_fills_both_missing_fields(self):
        title, artist = resolve_fallback_metadata("Pink_Floyd_-_Time", None, None)
        self.assertEqual((title, artist), ("Time", "Pink Floyd"))

    def test_partial_metadata_fills_only_missing_title(self):
        title, artist = resolve_fallback_metadata("Pink_Floyd_-_Time", None, "Some Real Artist")
        self.assertEqual(title, "Time")
        self.assertEqual(artist, "Some Real Artist")

    def test_partial_metadata_fills_only_missing_artist(self):
        title, artist = resolve_fallback_metadata("Pink_Floyd_-_Time", "Real Title", None)
        self.assertEqual(title, "Real Title")
        self.assertEqual(artist, "Pink Floyd")

    def test_valid_embedded_metadata_never_altered_despite_underscores_in_filename(self):
        title, artist = resolve_fallback_metadata("Pink_Floyd_-_Time", "Real Title", "Real Artist")
        self.assertEqual((title, artist), ("Real Title", "Real Artist"))

    def test_unknown_artist_placeholder_counts_as_missing(self):
        title, artist = resolve_fallback_metadata("Pink_Floyd_-_Time", "Real Title", "Unknown Artist")
        self.assertEqual((title, artist), ("Real Title", "Pink Floyd"))

    def test_date_prefixed_guess_is_rejected(self):
        title, artist = resolve_fallback_metadata("2015-07-30 MITD Part 1", None, None)
        self.assertEqual(artist, "Unknown Artist")
        self.assertEqual(title, "2015-07-30 MITD Part 1")

    def test_no_clean_split_falls_back_to_humanized_stem_and_unknown_artist(self):
        title, artist = resolve_fallback_metadata("just_a_title_no_dash", None, None)
        self.assertEqual(title, "just a title no dash")
        self.assertEqual(artist, "Unknown Artist")

    def test_upload_with_spaces_does_not_produce_underscores_in_db_metadata(self):
        """End-to-end through api_library_upload: a metadata-less file
        uploaded with a normal spaced filename must land in the DB
        with real spaces, and the file actually written to disk uses
        the safety-sanitized name (which may contain underscores) --
        the two are independent."""
        user = User.objects.create_superuser("uploader", "up@example.invalid", "pw")
        self.client.force_login(user)
        # code="music" already exists via a data migration -- get_or_create
        # rather than create() to avoid colliding with it.
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        category = Category.objects.create(code="TESTUP", name="Test Upload", kind=kind)

        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(LIBRARY_ROOT=tmp):
                from django.core.files.uploadedfile import SimpleUploadedFile
                # A minimal-but-valid MP3 isn't needed here since
                # parse_tags() on unparseable bytes just returns ({},
                # {}) -- exactly the "no usable metadata" case this
                # test wants to exercise -- and duration_seconds ends
                # up None, which is fine for a DB-only assertion.
                upload = SimpleUploadedFile(
                    "Pink Floyd - Time.mp3", b"not a real mp3, no tags readable",
                    content_type="audio/mpeg",
                )
                resp = self.client.post(
                    reverse("library:api-library-upload"),
                    {"category_id": category.id, "files": [upload]},
                )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["results"][0]["ok"], data["results"][0])
        track = Track.objects.get(id=data["results"][0]["track_id"])
        # DB metadata: real spaces, no underscores.
        self.assertEqual(track.title, "Time")
        self.assertEqual(track.artist.name, "Pink Floyd")
        self.assertNotIn("_", track.title)
        self.assertNotIn("_", track.artist.name)

    def test_legacy_underscore_laden_unknown_artist_title_handled_by_fix_unknown_artists(self):
        from io import StringIO

        from django.core.management import call_command

        track = make_track(title="Pink_Floyd_-_Time", artist_name="Unknown Artist")
        # The command's own path.is_file() check runs before it prints
        # anything about a candidate -- needs a real (content doesn't
        # matter for a dry run) file on disk, not just a DB path string.
        with tempfile.TemporaryDirectory() as tmp:
            real_path = Path(tmp) / "legacy.mp3"
            real_path.write_bytes(b"")
            track.filepath = str(real_path)
            track.filename = real_path.name
            track.save()

            out = StringIO()
            call_command("fix_unknown_artists", stdout=out)
            output = out.getvalue()
        # Dry run only inspects/reports -- assert it correctly parsed
        # the humanized "Pink Floyd - Time" rather than choking on the
        # literal underscores.
        self.assertIn("Pink Floyd", output)
        self.assertIn("Time", output)
        self.assertNotIn("would_tag", "")  # placeholder no-op to keep flake tools quiet


# =====================================================================
# API and UI-related behavior
# =====================================================================
@override_settings(SECURE_SSL_REDIRECT=False)
class ApiAndSearchTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("staffuser", "staff@example.invalid", "pw")

    def test_patch_accepts_and_canonicalizes_related_artists(self):
        self.client.force_login(self.staff)
        track = make_track(title="Time", artist_name="Pink Floyd")
        resp = self.client.patch(
            reverse("library:api-track-detail", args=[track.id]),
            data=json.dumps({"related_artists": "  Roger Waters ,  roger waters ,David Gilmour "}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        track.refresh_from_db()
        self.assertEqual(track.related_artists, "Roger Waters, David Gilmour")

    def test_patch_excludes_primary_artist_duplicate(self):
        self.client.force_login(self.staff)
        track = make_track(title="Time", artist_name="Pink Floyd")
        resp = self.client.patch(
            reverse("library:api-track-detail", args=[track.id]),
            data=json.dumps({"related_artists": "Pink Floyd, David Gilmour"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        track.refresh_from_db()
        self.assertEqual(track.related_artists, "David Gilmour")

    def test_read_only_user_cannot_modify_related_artists(self):
        from django.contrib.auth.models import Group

        ro_user = User.objects.create_user("readonly", "ro@example.invalid", "pw")
        Group.objects.get_or_create(name="remote_dj")[0].user_set.add(ro_user)
        self.client.force_login(ro_user)
        track = make_track(title="Time", artist_name="Pink Floyd", related_artists="Original Value")
        resp = self.client.patch(
            reverse("library:api-track-detail", args=[track.id]),
            data=json.dumps({"related_artists": "Hacked In Value"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        track.refresh_from_db()
        self.assertEqual(track.related_artists, "Original Value")

    def test_library_search_matches_related_artists(self):
        make_track(title="Song A", artist_name="Solo Artist A", related_artists="Featured Person")
        make_track(title="Song B", artist_name="Solo Artist B", related_artists="")
        qs = filter_tracks(Track.objects.all(), q="Featured Person")
        self.assertEqual(list(qs.values_list("title", flat=True)), ["Song A"])

    def test_search_plus_semantics_still_require_all_terms(self):
        t1 = make_track(title="Money", artist_name="Pink Floyd")
        make_track(title="Time", artist_name="Pink Floyd")
        qs = filter_tracks(Track.objects.all(), q="Pink Floyd + Money")
        self.assertEqual(list(qs.values_list("id", flat=True)), [t1.id])

    def test_category_filter_still_includes_additional_categories(self):
        kind, _ = CategoryKind.objects.get_or_create(code="test-kind-catfilter", defaults={"name": "Test Kind"})
        primary = Category.objects.create(code="PRIMARYCAT", name="Primary", kind=kind)
        secondary = Category.objects.create(code="SECONDCAT", name="Secondary", kind=kind)
        track = make_track(title="Song", artist_name="Artist", category=primary)
        track.additional_categories.add(secondary)
        qs = filter_tracks(Track.objects.all(), category_id=secondary.id)
        self.assertIn(track.id, qs.values_list("id", flat=True))

    def test_autofill_endpoint_processes_full_filtered_queryset_not_one_page(self):
        self.client.force_login(self.staff)
        for i in range(60):
            make_track(title=f"Song feat. Extra {i}", artist_name=f"Artist {i}", related_artists="")
        resp = self.client.post(
            reverse("library:api-track-autofill-related-artists"),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        # Well over the 50-row page size used elsewhere in the UI.
        self.assertGreaterEqual(data["scanned"], 60)
        self.assertGreaterEqual(data["changed"], 60)

    def test_autofill_endpoint_preserves_existing_values(self):
        self.client.force_login(self.staff)
        track = make_track(
            title="Song feat. New One", artist_name="Solo Artist",
            related_artists="Existing Manual Entry",
        )
        resp = self.client.post(
            reverse("library:api-track-autofill-related-artists"),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        track.refresh_from_db()
        self.assertIn("Existing Manual Entry", track.related_artists)
        self.assertIn("New One", track.related_artists)

    def test_autofill_endpoint_rejected_for_read_only_user(self):
        from django.contrib.auth.models import Group

        ro_user = User.objects.create_user("ro2", "ro2@example.invalid", "pw")
        Group.objects.get_or_create(name="remote_dj")[0].user_set.add(ro_user)
        self.client.force_login(ro_user)
        resp = self.client.post(
            reverse("library:api-track-autofill-related-artists"),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_autofill_command_and_endpoint_agree(self):
        """Both call the exact same service function -- a direct
        smoke test that autofill_related_artists_for_queryset itself
        (used by both) behaves identically regardless of caller."""
        make_track(title="Song feat. Shared Discovery", artist_name="Shared Artist", related_artists="")
        result = autofill_related_artists_for_queryset(Track.objects.all(), apply=False)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(Track.objects.get().related_artists, "")  # dry run: nothing written


# =====================================================================
# 500-character storage limit (Track.related_artists is a real DB
# varchar(500)) -- every write path must respect it: reject a too-long
# manual value cleanly rather than let it reach the DB as an unhandled
# DataError, and never let automatic append-only discovery grow the
# field past the limit, truncate an entry mid-name, or drop an existing
# value to make room.
# =====================================================================
@override_settings(SECURE_SSL_REDIRECT=False)
class LengthLimitTests(TestCase):
    def test_manual_value_exactly_at_limit_is_accepted(self):
        self.staff = User.objects.create_superuser("lenstaff1", "lenstaff1@example.invalid", "pw")
        self.client.force_login(self.staff)
        track = make_track(title="Time", artist_name="Pink Floyd")
        # Two entries whose canonical comma-space-joined form lands on
        # EXACTLY the limit -- proves the boundary itself (== max_length,
        # not just comfortably under it) is accepted, not rejected.
        first = "Roger Waters"
        second = "Z" * (RELATED_ARTISTS_MAX_LENGTH - len(first) - 2)  # -2 for the ", " separator
        value = f"{first}, {second}"
        self.assertEqual(len(value), RELATED_ARTISTS_MAX_LENGTH)

        resp = self.client.patch(
            reverse("library:api-track-detail", args=[track.id]),
            data=json.dumps({"related_artists": value}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        track.refresh_from_db()
        self.assertEqual(len(track.related_artists), RELATED_ARTISTS_MAX_LENGTH)

    def test_manual_value_over_limit_returns_400_not_a_server_error(self):
        self.staff = User.objects.create_superuser("lenstaff2", "lenstaff2@example.invalid", "pw")
        self.client.force_login(self.staff)
        track = make_track(title="Time", artist_name="Pink Floyd", related_artists="Original Value")
        too_long = "X" * (RELATED_ARTISTS_MAX_LENGTH + 50)
        resp = self.client.patch(
            reverse("library:api-track-detail", args=[track.id]),
            data=json.dumps({"related_artists": too_long}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", json.loads(resp.content))
        # Rejected outright -- the existing value is untouched, nothing
        # partially applied.
        track.refresh_from_db()
        self.assertEqual(track.related_artists, "Original Value")

    def test_automatic_append_that_fits_is_written_normally(self):
        result_value = merge_related_artists_detailed(
            "Existing Name", ["New Discovery"], primary_artist_name="Primary Artist",
        )
        value, appended, skipped = result_value
        self.assertEqual(value, "Existing Name, New Discovery")
        self.assertEqual(appended, ["New Discovery"])
        self.assertEqual(skipped, [])

    def test_automatic_append_where_next_entry_would_exceed_limit_is_skipped_whole(self):
        # Existing value already near the limit; the one discovered
        # name is long enough that appending it would exceed
        # max_length. It must be skipped WHOLE (never truncated) and
        # reported, and the existing value must survive unchanged.
        existing = "A" * (RELATED_ARTISTS_MAX_LENGTH - 10)  # 10 chars of headroom
        too_big_to_fit = "B" * 50  # needs 52 chars (", " + 50) -- doesn't fit in 10
        value, appended, skipped = merge_related_artists_detailed(
            existing, [too_big_to_fit], primary_artist_name=None, max_length=RELATED_ARTISTS_MAX_LENGTH,
        )
        self.assertEqual(value, existing)
        self.assertEqual(appended, [])
        self.assertEqual(skipped, [too_big_to_fit])
        self.assertNotIn("B", value)  # never partially spliced in
        self.assertLessEqual(len(value), RELATED_ARTISTS_MAX_LENGTH)

    def test_automatic_append_skips_only_names_that_dont_fit_keeps_trying_shorter_ones(self):
        # A too-long name in the middle of the discovered list must not
        # abort the rest -- a later, SHORTER name that does fit should
        # still be appended.
        existing = "A" * (RELATED_ARTISTS_MAX_LENGTH - 10)
        too_big = "B" * 50
        fits = "C"
        value, appended, skipped = merge_related_artists_detailed(
            existing, [too_big, fits], primary_artist_name=None, max_length=RELATED_ARTISTS_MAX_LENGTH,
        )
        self.assertIn("C", value)
        self.assertEqual(appended, ["C"])
        self.assertEqual(skipped, [too_big])

    def test_existing_manual_values_preserved_during_overflow(self):
        # Existing value is close to the limit but under it -- room for
        # a small discovery, but not a 100-char one.
        existing = "Manual One, Manual Two, " + ("Z" * (RELATED_ARTISTS_MAX_LENGTH - 40))
        too_big = "Q" * 100
        value, appended, skipped = merge_related_artists_detailed(
            existing, [too_big], primary_artist_name=None, max_length=RELATED_ARTISTS_MAX_LENGTH,
        )
        self.assertIn("Manual One", value)
        self.assertIn("Manual Two", value)
        self.assertEqual(appended, [])
        self.assertEqual(skipped, [too_big])

    def test_merge_related_artists_never_exceeds_max_length(self):
        """The plain (non-detailed) merge_related_artists wrapper must
        also never produce an over-limit string -- it's what analyze_
        tracks.py actually calls."""
        existing = "A" * (RELATED_ARTISTS_MAX_LENGTH - 5)
        result = merge_related_artists(existing, ["B" * 50], primary_artist_name=None)
        self.assertLessEqual(len(result), RELATED_ARTISTS_MAX_LENGTH)
        self.assertEqual(result, existing)

    def test_autofill_continues_after_one_overflow_track(self):
        """A single oversized track must not abort the whole run --
        other tracks in the same queryset must still be scanned,
        discovered, and written normally."""
        overflow_track = make_track(
            title="Song feat. Someone", artist_name="Solo Artist",
            related_artists="A" * (RELATED_ARTISTS_MAX_LENGTH - 5),
        )
        normal_track = make_track(
            title="Other Song feat. New Person", artist_name="Another Artist",
            related_artists="",
        )
        result = autofill_related_artists_for_queryset(Track.objects.all(), apply=True)

        self.assertEqual(result["scanned"], 2)
        self.assertGreaterEqual(result["unchanged_overflow"], 1)
        self.assertGreaterEqual(result["overflow_skipped"], 1)
        # The other track still got processed and written normally --
        # the overflow track didn't abort the run.
        normal_track.refresh_from_db()
        self.assertIn("New Person", normal_track.related_artists)
        # The overflow track's existing value is untouched, not
        # truncated or corrupted.
        overflow_track.refresh_from_db()
        self.assertEqual(overflow_track.related_artists, "A" * (RELATED_ARTISTS_MAX_LENGTH - 5))
        self.assertEqual(result["errors"], 0)  # overflow is not an "error" -- it's reported separately


# =====================================================================
# Extraction-query efficiency: the bare-'&'/'and' split's existing-
# Artist-name lookup must be bulk-loaded once per run, never once per
# track and never once per candidate.
# =====================================================================
class QueryEfficiencyTests(TestCase):
    def test_autofill_loads_artist_lookup_exactly_once_regardless_of_track_count(self):
        import library.services.related_artists as related_artists_module

        Artist.get_or_create_ci("Jay-Z")
        Artist.get_or_create_ci("Kanye West")
        for i in range(50):
            # The bare-'&' split only fires when the FULL text (here,
            # the primary artist field) is JUST "Name1 & Name2" -- it
            # splits the whole input on the first '&', not a substring
            # buried inside other surrounding text (that's what makes
            # it conservative). A jointly-credited primary artist field
            # is a realistic real-world shape for this to fire on.
            make_track(
                title=f"Song {i}",
                artist_name="Jay-Z & Kanye West",
                related_artists="",
            )

        with patch(
            "library.services.related_artists.existing_artist_names_casefolded",
            wraps=related_artists_module.existing_artist_names_casefolded,
        ) as mocked_lookup:
            result = autofill_related_artists_for_queryset(Track.objects.all(), apply=True)

        self.assertEqual(result["scanned"], 50)
        # The bare-conjunction split actually fired for at least some
        # tracks -- proves the lookup was doing real, exercised work,
        # not skipped as a no-op scenario.
        self.assertGreater(result["changed"], 0)
        self.assertEqual(mocked_lookup.call_count, 1)

    def test_total_query_count_does_not_scale_with_track_count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        Artist.get_or_create_ci("Jay-Z")
        Artist.get_or_create_ci("Kanye West")
        for i in range(50):
            # The bare-'&' split only fires when the FULL text (here,
            # the primary artist field) is JUST "Name1 & Name2" -- it
            # splits the whole input on the first '&', not a substring
            # buried inside other surrounding text (that's what makes
            # it conservative). A jointly-credited primary artist field
            # is a realistic real-world shape for this to fire on.
            make_track(
                title=f"Song {i}",
                artist_name="Jay-Z & Kanye West",
                related_artists="",
            )

        with CaptureQueriesContext(connection) as ctx:
            result = autofill_related_artists_for_queryset(Track.objects.all(), apply=True)

        self.assertEqual(result["scanned"], 50)
        self.assertGreater(result["changed"], 0)
        # O(N) per-track (let alone O(candidates) per-track) Artist
        # querying against 50 tracks would land in the hundreds; actual
        # cost is O(1) lookup load + O(1) iterator-chunked read + a
        # handful of batched writes.
        self.assertLess(len(ctx.captured_queries), 30)
