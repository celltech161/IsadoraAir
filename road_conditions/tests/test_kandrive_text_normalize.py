"""road_conditions/text_normalize.py tests -- deterministic speech-text
transforms only (no generative model, no network, no Kokoro). Covers
route notation, direction abbreviations, LinkDirection mapping, time/
date/misc normalization, internal-admin-text stripping, and the near-
duplicate sentence filter (including the two real bugs found and fixed
against live data: the "U.S." sentence-split fragmentation, and the
trailing-punctuation exact-duplicate miss)."""
from django.test import SimpleTestCase

from road_conditions.text_normalize import (
    dedupe_similar_sentences,
    link_direction_to_words,
    normalize_direction_abbreviations,
    normalize_for_speech,
    normalize_route_notation,
    normalize_time_and_misc,
    strip_internal_admin_text,
)


class RouteNotationTests(SimpleTestCase):
    def test_interstate_hyphen_and_space_and_bare_forms(self):
        self.assertEqual(normalize_route_notation("I-70 westbound"), "Interstate 70 westbound")
        self.assertEqual(normalize_route_notation("I 70 westbound"), "Interstate 70 westbound")
        self.assertEqual(normalize_route_notation("I70 westbound"), "Interstate 70 westbound")

    def test_us_route(self):
        self.assertEqual(normalize_route_notation("US 81 in both directions"), "U.S. 81 in both directions")
        self.assertEqual(normalize_route_notation("US-81"), "U.S. 81")

    def test_ks_route(self):
        self.assertEqual(normalize_route_notation("KS 15: Road closed"), "Kansas Highway 15: Road closed")
        self.assertEqual(normalize_route_notation("KS-15"), "Kansas Highway 15")

    def test_k_dash_defensive_shorthand(self):
        self.assertEqual(normalize_route_notation("K-15 closed"), "Kansas Highway 15 closed")

    def test_does_not_misfire_on_unrelated_text(self):
        # "US" and "KS" as bare words (no trailing route number) must
        # never be rewritten -- these patterns require a number right
        # after the prefix.
        self.assertEqual(normalize_route_notation("US Bank Plaza"), "US Bank Plaza")
        self.assertEqual(normalize_route_notation("KS Turnpike Authority"), "KS Turnpike Authority")


class DirectionAbbreviationTests(SimpleTestCase):
    def test_whole_word_abbreviations(self):
        self.assertEqual(normalize_direction_abbreviations("I-70 EB"), "I-70 eastbound")
        self.assertEqual(normalize_direction_abbreviations("NB lane closed"), "northbound lane closed")
        self.assertEqual(normalize_direction_abbreviations("SB and WB affected"), "southbound and westbound affected")

    def test_does_not_misfire_inside_a_word(self):
        # NEB/SEB/etc contain the letter pairs but must not match --
        # \b anchoring on both sides prevents this.
        self.assertEqual(normalize_direction_abbreviations("NEBRASKA"), "NEBRASKA")


class LinkDirectionMappingTests(SimpleTestCase):
    def test_known_values(self):
        self.assertEqual(link_direction_to_words("N"), "northbound")
        self.assertEqual(link_direction_to_words("BOTH_DIRECTIONS"), "in both directions")
        self.assertEqual(link_direction_to_words("both directions"), "in both directions")
        self.assertEqual(link_direction_to_words("NORTHBOUND"), "northbound")

    def test_administrative_values_map_to_empty(self):
        self.assertEqual(link_direction_to_words("NOT_DIRECTIONAL"), "")
        self.assertEqual(link_direction_to_words("ANY_OTHER"), "")

    def test_blank_and_unknown_values_degrade_to_empty_not_raise(self):
        self.assertEqual(link_direction_to_words(""), "")
        self.assertEqual(link_direction_to_words(None), "")
        self.assertEqual(link_direction_to_words("SOME_FUTURE_KDOT_VALUE"), "")


class TimeAndMiscNormalizationTests(SimpleTestCase):
    def test_twenty_four_seven(self):
        self.assertEqual(normalize_time_and_misc("Open 24/7"), "Open twenty four seven")

    def test_slash_between_county_names(self):
        self.assertEqual(
            normalize_time_and_misc("Clay County Line/Dickinson County Line"),
            "Clay County Line and Dickinson County Line",
        )

    def test_slash_between_digits_is_untouched(self):
        # Only fires between two alphabetic words, never between digits
        # -- a genuine fraction-like token (e.g. a date fragment) must
        # survive untouched.
        self.assertEqual(normalize_time_and_misc("5/27/26"), "5/27/26")

    def test_decimal_point_spoken_as_point(self):
        self.assertEqual(normalize_time_and_misc("Width limit 12.5 feet"), "Width limit 12 point 5 feet")

    def test_ampm_spacing(self):
        self.assertEqual(normalize_time_and_misc("11:59PM CDT"), "11:59 PM CDT")
        self.assertEqual(normalize_time_and_misc("2:45 PM CDT"), "2:45 PM CDT")

    def test_collapses_repeated_whitespace(self):
        self.assertEqual(normalize_time_and_misc("A   B\n\nC"), "A B C")


class StripInternalAdminTextTests(SimpleTestCase):
    def test_strips_next_update_time_clause(self):
        text = "KS 14: Bridge construction. Width limit 12'0\". Next update time 2:45 PM CDT, 5/27/26."
        self.assertEqual(
            strip_internal_admin_text(text),
            "KS 14: Bridge construction. Width limit 12'0\".",
        )

    def test_strips_see_map_for_detour_reference(self):
        text = (
            "KS 15: Road closed. The road is closed due to bridge construction work. "
            "Please follow the Kansas DOT-recommended detour around the closure. See map for detour(s)."
        )
        result = strip_internal_admin_text(text)
        self.assertNotIn("See map", result)
        self.assertIn("Please follow the Kansas DOT-recommended detour around the closure.", result)

    def test_does_not_strip_a_real_timing_statement(self):
        text = "Until November 13, 2026 at about 11:59 PM CDT."
        self.assertEqual(strip_internal_admin_text(text), text)


class DedupeSimilarSentencesTests(SimpleTestCase):
    def test_exact_duplicate_short_sentence_removed(self):
        text = "Width limit 12'0\". Some other content here now. Width limit 12'0\"."
        result = dedupe_similar_sentences(text)
        self.assertEqual(result.count("Width limit"), 1)

    def test_us_route_sentence_split_does_not_corrupt_output(self):
        """Regression test for the real bug found against live data:
        normalize_route_notation turns "US 24" into "U.S. 24", which
        contains its own ". " -- a naive split used to break that one
        sentence into two fragments, and when only one fragment of a
        duplicated pair got removed, the survivor was left dangling
        mid-sentence. Must never produce a truncated "...and U.S" with
        no continuation."""
        text = (
            "Between Kansas Highway 194; Cloud County Line and Mitchell County Line and U.S. 81 "
            "(near the Asherville area). Bridge construction work is in progress. "
            "Between Kansas Highway 194; Cloud County Line and Mitchell County Line and U.S. 81 "
            "(near the Asherville area). Bridge construction work is in progress."
        )
        result = dedupe_similar_sentences(text)
        self.assertNotIn("and U.S\n", result)
        self.assertFalse(result.rstrip().endswith("U.S"))
        self.assertIn("U.S. 81 (near the Asherville area)", result)
        # The whole second, identical restatement should collapse away.
        self.assertEqual(result.count("Bridge construction work is in progress"), 1)

    def test_trailing_punctuation_does_not_defeat_exact_duplicate_detection(self):
        """Regression test for the real bug found against live data:
        the LAST sentence in a description keeps its trailing period
        (nothing follows to consume it as a separator) while an
        identical EARLIER occurrence does not -- word-set comparison
        must catch this even though the raw strings differ by a
        trailing "."."" """
        text = 'Width limit 12\'0". Some unrelated filler sentence words here. Width limit 12\'0".'
        result = dedupe_similar_sentences(text)
        self.assertEqual(result.count("Width limit"), 1)

    def test_comment_section_recombining_earlier_facts_is_dropped(self):
        """The real, verified KDOT pattern: a Comment section restates
        route+boundary using different punctuation/word order, pulling
        pieces from more than one earlier sentence -- cumulative
        containment (not pairwise Jaccard) is required to catch this."""
        text = (
            "Kansas Highway 14: Bridge construction. "
            "Between Lincoln County Line and Mitchell County Line and U.S. 24 (Beloit). "
            "Bridge construction work is in progress. "
            "Width limit 12'0\". "
            "Comment: Kansas Highway 14, between Lincoln County Line and Mitchell County Line and U.S. 24 (Beloit). "
            "Bridge construction work is in progress. "
            "Width limit 12'0\"."
        )
        result = dedupe_similar_sentences(text)
        self.assertNotIn("Comment:", result)
        self.assertEqual(result.count("Width limit"), 1)
        self.assertEqual(result.count("Bridge construction work is in progress"), 1)

    def test_genuinely_new_comment_content_is_preserved(self):
        """A Comment that adds real, distinct facts (a different set of
        exit numbers) must survive -- this is exactly the case the
        task's "do not invent facts... by omission" principle protects
        against losing."""
        text = (
            "Interstate 135 in both directions: Shoulder closed. The shoulder is closed. "
            "Comment: There will be some shoulder and partial ramp lane closures near the "
            "interchanges at exit 86, 88, 89, 90, and 92."
        )
        result = dedupe_similar_sentences(text)
        self.assertIn("exit 86, 88, 89, 90, and 92", result)

    def test_short_distinct_sentence_is_not_dropped(self):
        text = "Bridge construction work is in progress. Use caution."
        result = dedupe_similar_sentences(text)
        self.assertIn("Use caution", result)

    def test_different_numeric_fact_is_not_conflated(self):
        text = "Width limit 12'0\". Width limit 10'0\"."
        result = dedupe_similar_sentences(text)
        self.assertIn("12'0", result)
        self.assertIn("10'0", result)


class FullPipelineIntegrationTests(SimpleTestCase):
    def test_real_cars5_10771_description_fully_normalized(self):
        """The exact real (sanitized) description that surfaced both
        bugs fixed in this module, run through the whole pipeline."""
        raw = (
            "KS 14: Bridge construction. Between Lincoln County Line/Mitchell County Line and US 24 (Beloit). "
            "Bridge construction work is in progress. Width limit 12'0\". "
            "Comment: KS 14, between Lincoln County Line/Mitchell County Line and US 24 (Beloit). "
            "Bridge construction work is in progress. Width limit 12'0\". "
            "Next update time 2:45 PM CDT, 5/27/26."
        )
        result = normalize_for_speech(raw)
        self.assertEqual(
            result,
            "Kansas Highway 14: Bridge construction. Between Lincoln County Line and Mitchell County Line "
            "and U.S. 24 (Beloit). Bridge construction work is in progress. Width limit 12'0\"",
        )

    def test_empty_and_none_input(self):
        self.assertEqual(normalize_for_speech(""), "")
        self.assertEqual(normalize_for_speech(None), "")

    def test_idempotent(self):
        raw = "KS 15: Road closed. NB and SB affected. 24/7. Width limit 12.5 feet."
        once = normalize_for_speech(raw)
        twice = normalize_for_speech(once)
        self.assertEqual(once, twice)
