"""Regression coverage for the production Kokoro text normalizer."""

from django.test import SimpleTestCase

from isadoraair.tts.normalization import preprocess_text


class KokoroNormalizationTests(SimpleTestCase):
    def test_decimal_points_between_digit_runs_are_spoken(self):
        self.assertEqual(
            preprocess_text("Water is 20.2 feet and changed 0.75 feet."),
            "Water is 20 point 2 feet and changed 0 point 75 feet.",
        )

    def test_sentence_periods_and_colon_times_are_unchanged(self):
        self.assertEqual(preprocess_text("At 4:36 PM. Stay ready."), "At 4:36 PM. Stay ready.")

    def test_every_decimal_separator_in_a_digit_chain_is_spoken(self):
        self.assertEqual(preprocess_text("Version 1.2.3"), "Version 1 point 2 point 3")

    def test_supported_us_phone_number_forms_are_worded_digit_by_digit(self):
        expected = "Call three five two, seven three two, nine one one one"
        for number in (
            "(352) 732-9111",
            "352-732-9111",
            "352.732.9111",
            "352 732 9111",
            "352732-9111",
        ):
            with self.subTest(number=number):
                self.assertEqual(preprocess_text(f"Call {number}"), expected)

    def test_phone_number_must_have_final_separator_and_digit_boundaries(self):
        self.assertEqual(preprocess_text("3527329111"), "3527329111")
        self.assertEqual(preprocess_text("1352-732-9111"), "1352-732-9111")

    def test_phone_subscriber_digits_are_not_reprocessed_as_emergency_number(self):
        self.assertEqual(
            preprocess_text("352-732-9111"),
            "three five two, seven three two, nine one one one",
        )

    def test_standalone_911_is_spoken_digit_by_digit(self):
        self.assertEqual(preprocess_text("Call 911 now; not 9111."), "Call nine one one now; not 9111.")

    def test_hashtags_are_removed_across_the_body(self):
        self.assertEqual(
            preprocess_text("#FLAMBER Seek shelter #TornadoWarning immediately."),
            "Seek shelter immediately.",
        )

    def test_hashtag_pattern_preserves_production_punctuation_behavior(self):
        self.assertEqual(preprocess_text("Alert #FL_AMBER2."), "Alert .")

    def test_whitespace_is_always_collapsed_and_trimmed(self):
        self.assertEqual(preprocess_text("  first\n\tsecond   third  "), "first second third")
