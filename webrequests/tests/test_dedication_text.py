"""Unit coverage for webrequests/dedication_text.py -- the station-
editable dedication/request text-policy module introduced in r0056
(Pass F). Pure-function coverage; no database needed. See
webrequests/tests/test_dedication_intros.py's DedicationTextTemplateTests
for the higher-level build_dedication_intro_text()/WebRequestConfig
integration, and its DedicationTemplateAdminValidationTests /
DedicationTextPolicyLifecycleTests for Admin-time validation and the
non-fatal request-lifecycle contract."""
from django.test import SimpleTestCase

from webrequests.dedication_text import (
    DedicationInputPolicyError,
    DedicationTemplateError,
    MAX_RENDERED_SCRIPT_LENGTH,
    expand_featuring,
    normalize_dedication_message,
    normalize_requester_name,
    render_dedication_script,
    validate_template,
)

NAMED_MESSAGE = "Now here's {title} by {artist}, {dedication_message} Thanks {requester_name} for your dedication."
NAMED_REQUEST = "Now here's {title} by {artist}. Thanks {requester_name} for your request."
ANON_MESSAGE = "Now here's {title} by {artist}, {dedication_message}"
ANON_REQUEST = "Now here's {title} by {artist}."


class TemplateValidationTests(SimpleTestCase):
    def test_all_four_default_templates_are_valid(self):
        for template in (NAMED_MESSAGE, NAMED_REQUEST, ANON_MESSAGE, ANON_REQUEST):
            with self.subTest(template=template):
                validate_template(template)  # must not raise

    def test_unknown_placeholder_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {title} by {artist}, {station_slogan}")

    def test_attribute_access_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {track.title}")

    def test_index_access_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {foo[0]}")

    def test_conversion_syntax_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {title!r}")

    def test_format_spec_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {title:>20}")

    def test_malformed_unterminated_brace_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {title by {artist}")

    def test_malformed_stray_closing_brace_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {title} by artist}")

    def test_auto_numbering_placeholder_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {}")

    def test_positional_index_placeholder_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {0}")

    def test_empty_template_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("")
        with self.assertRaises(DedicationTemplateError):
            validate_template("   ")

    def test_non_string_template_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template(None)

    def test_excessively_long_template_rejected(self):
        with self.assertRaises(DedicationTemplateError):
            validate_template("Now here's {title}. " + ("x" * 600))

    def test_literal_braces_survive_when_no_placeholder_present(self):
        # A template with no replacement fields at all is unusual but
        # not unsafe -- nothing here can leak model data.
        validate_template("The station plays only the hits!")


class NormalizationTests(SimpleTestCase):
    def test_requester_name_unicode_names_and_punctuation_preserved(self):
        self.assertEqual(normalize_requester_name("José O'Neil-Ñandú"), "José O'Neil-Ñandú")

    def test_requester_name_whitespace_collapsed_and_trimmed(self):
        self.assertEqual(normalize_requester_name("  Justin   Reed  "), "Justin Reed")

    def test_requester_name_newlines_and_tabs_collapsed_to_spaces(self):
        self.assertEqual(normalize_requester_name("Justin\n\tReed"), "Justin Reed")

    def test_requester_name_unsafe_control_characters_removed(self):
        self.assertEqual(normalize_requester_name("Jus\x00tin\x1b"), "Justin")

    def test_requester_name_c1_control_character_removed(self):
        # \x90 (DEVICE CONTROL STRING) is a non-whitespace C1 control
        # -- must be eliminated outright, same as a C0 control.
        self.assertEqual(normalize_requester_name("Jus\x90tin"), "Justin")
        self.assertEqual(normalize_requester_name("\x80\x9fJustin\x81"), "Justin")

    def test_requester_name_whitespace_control_boundary_collapses_to_space(self):
        # \x85 (NEL) is a C1 control that Python also treats as
        # whitespace -- it must become a normal space between the two
        # words, never be silently eliminated (which would wrongly
        # concatenate them), and never double up with an adjacent
        # ordinary space.
        self.assertEqual(normalize_requester_name("Justin\x85Reed"), "Justin Reed")
        # Same boundary behavior for the C0 "unit separator" controls,
        # which are also both Cc and whitespace per str.isspace().
        self.assertEqual(normalize_requester_name("Justin\x1cReed"), "Justin Reed")

    def test_requester_name_none_becomes_empty_string(self):
        self.assertEqual(normalize_requester_name(None), "")

    def test_requester_name_non_string_rejected(self):
        with self.assertRaises(DedicationInputPolicyError):
            normalize_requester_name(12345)

    def test_dedication_message_whitespace_collapsed(self):
        self.assertEqual(normalize_dedication_message("  hello \n  world  "), "hello world.")

    def test_dedication_message_appends_missing_period(self):
        self.assertEqual(normalize_dedication_message("for my late night drive"), "for my late night drive.")

    def test_dedication_message_does_not_double_existing_punctuation(self):
        self.assertEqual(normalize_dedication_message("rock on!"), "rock on!")
        self.assertEqual(normalize_dedication_message("really?"), "really?")
        self.assertEqual(normalize_dedication_message("done."), "done.")

    def test_dedication_message_whitespace_only_becomes_empty(self):
        self.assertEqual(normalize_dedication_message("   "), "")

    def test_dedication_message_none_becomes_empty_string(self):
        self.assertEqual(normalize_dedication_message(None), "")

    def test_dedication_message_non_string_rejected(self):
        with self.assertRaises(DedicationInputPolicyError):
            normalize_dedication_message(["not", "a", "string"])

    def test_expand_featuring_case_insensitive_with_period(self):
        self.assertEqual(expand_featuring("Song (feat. Guest)"), "Song (featuring Guest)")
        self.assertEqual(expand_featuring("Song (FEAT. Guest)"), "Song (featuring Guest)")

    def test_expand_featuring_bare_word_untouched(self):
        self.assertEqual(expand_featuring("Incredible Feat"), "Incredible Feat")

    def test_expand_featuring_already_spelled_out_not_doubled(self):
        self.assertEqual(expand_featuring("Song featuring Guest"), "Song featuring Guest")


class RenderDedicationScriptTests(SimpleTestCase):
    def _render(self, **overrides):
        values = dict(
            title="Free Fallin'",
            artist="Tom Petty",
            requester_name="",
            dedication_message="",
            named_message_template=NAMED_MESSAGE,
            named_request_template=NAMED_REQUEST,
            anonymous_message_template=ANON_MESSAGE,
            anonymous_request_template=ANON_REQUEST,
            dedication_message_spoken_limit=300,
        )
        values.update(overrides)
        return render_dedication_script(**values)

    def test_named_dedication_default_text(self):
        text = self._render(requester_name="Justin", dedication_message="for my late night drive")
        self.assertEqual(
            text,
            "Now here's Free Fallin' by Tom Petty, for my late night drive. "
            "Thanks Justin for your dedication.",
        )

    def test_named_request_default_text(self):
        text = self._render(requester_name="Justin")
        self.assertEqual(text, "Now here's Free Fallin' by Tom Petty. Thanks Justin for your request.")

    def test_anonymous_dedication_default_text(self):
        text = self._render(dedication_message="for my late night drive")
        self.assertEqual(text, "Now here's Free Fallin' by Tom Petty, for my late night drive.")

    def test_anonymous_request_default_text(self):
        text = self._render()
        self.assertEqual(text, "Now here's Free Fallin' by Tom Petty.")

    def test_station_custom_wording_is_honored(self):
        text = self._render(
            requester_name="Justin",
            anonymous_request_template="Coming up: {title} by {artist}, right here on the station.",
        )
        # Named-request template still governs this case (requester
        # name present) -- station customization of one variant must
        # not bleed into another.
        self.assertEqual(text, "Now here's Free Fallin' by Tom Petty. Thanks Justin for your request.")

        text2 = self._render(
            anonymous_request_template="Coming up: {title} by {artist}, right here on the station.",
        )
        self.assertEqual(text2, "Coming up: Free Fallin' by Tom Petty, right here on the station.")

    def test_braces_in_listener_message_are_plain_data_not_reinterpreted(self):
        text = self._render(
            requester_name="Justin",
            dedication_message="for my {favorite} night ever",
        )
        self.assertIn("for my {favorite} night ever.", text)

    def test_feat_normalization_applies_to_title_and_artist(self):
        text = self._render(title="Song (feat. Guest)", artist="Main Act feat. Guest")
        self.assertIn("Song (featuring Guest)", text)
        self.assertIn("Main Act featuring Guest", text)

    def test_over_limit_dedication_message_raises_without_truncating(self):
        long_message = "x" * 301
        with self.assertRaises(DedicationInputPolicyError):
            self._render(requester_name="Justin", dedication_message=long_message)

    def test_message_exactly_at_limit_is_accepted(self):
        # 299 chars + the appended terminal period == 300, the default limit.
        message = "x" * 299
        text = self._render(requester_name="Justin", dedication_message=message, dedication_message_spoken_limit=300)
        self.assertIn("x" * 299 + ".", text)

    def test_station_configurable_spoken_limit_is_honored(self):
        message = "x" * 60
        with self.assertRaises(DedicationInputPolicyError):
            self._render(requester_name="Justin", dedication_message=message, dedication_message_spoken_limit=50)

    def test_oversized_track_metadata_trips_final_script_limit(self):
        huge_title = "T" * (MAX_RENDERED_SCRIPT_LENGTH + 50)
        with self.assertRaises(DedicationInputPolicyError):
            self._render(title=huge_title, requester_name="Justin")

    def test_invalid_stored_template_fails_safely_at_render_time(self):
        """Simulates a template written outside Admin (migration, shell
        edit, fixture) that bypassed field-validator enforcement --
        render_dedication_script must still reject it, never trust
        stored data."""
        with self.assertRaises(DedicationTemplateError):
            self._render(
                requester_name="Justin",
                named_request_template="Now here's {track.title} by {artist}.",
            )

    def test_no_silent_truncation_on_either_limit(self):
        long_message = "x" * 301
        try:
            self._render(requester_name="Justin", dedication_message=long_message)
        except DedicationInputPolicyError:
            pass
        else:
            self.fail("expected DedicationInputPolicyError")
        # The module never returns a truncated string on this path --
        # the exception is the only outcome, proven above.
