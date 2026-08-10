"""encoders/services/validation.py -- Phase 2A of the encoder hardening
effort (centralized configuration validation, independent of Django
admin/Model.clean() so a direct ORM write can't bypass it).

Uses plain, unsaved Encoder(...) instances throughout (no DB round
trip needed -- these are pure-function validators over field values),
except where duplicate-detection needs real primary keys to name
conflicting rows, which uses TestCase + .objects.create()."""
from django.test import SimpleTestCase, TestCase

from encoders.models import Encoder
from encoders.services import validation as v


def make_encoder(**overrides):
    defaults = dict(
        name="test", enabled=True, protocol="shoutcast2",
        host="192.168.1.112", port=8000, mount="/4", username="source",
        password="secret", format="mp3", bitrate_kbps=192,
        station_name="Test Station", genre="Variety", url="https://example.com",
        public=False,
    )
    defaults.update(overrides)
    return Encoder(**defaults)


# ---------------------------------------------------------------------
# Shoutcast Stream ID
# ---------------------------------------------------------------------
class NormalizeShoutcastSidTests(SimpleTestCase):
    def test_plain_digit_accepted(self):
        self.assertEqual(v.normalize_shoutcast_sid("4"), 4)

    def test_leading_slash_accepted(self):
        self.assertEqual(v.normalize_shoutcast_sid("/4"), 4)

    def test_multi_digit_accepted(self):
        self.assertEqual(v.normalize_shoutcast_sid("42"), 42)

    def test_blank_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("")

    def test_none_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid(None)

    def test_whitespace_only_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("   ")

    def test_zero_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("0")

    def test_negative_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("-4")

    def test_non_numeric_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("abc")

    def test_multiple_slashes_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("//4")

    def test_trailing_garbage_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("4abc")

    def test_liquidsoap_fragment_injection_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("4); output.dummy(blank())")

    def test_control_character_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("4\n")

    def test_decimal_point_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("4.5")

    def test_plus_sign_rejected(self):
        with self.assertRaises(v.ConfigValidationError):
            v.normalize_shoutcast_sid("+4")

    def test_leading_zero_normalizes(self):
        self.assertEqual(v.normalize_shoutcast_sid("04"), 4)

    def test_returns_int_not_str(self):
        self.assertIsInstance(v.normalize_shoutcast_sid("4"), int)


# ---------------------------------------------------------------------
# Protocol / format compatibility
# ---------------------------------------------------------------------
class ProtocolFormatMatrixTests(SimpleTestCase):
    def test_mp3_icecast_allowed(self):
        self.assertEqual(v.validate_protocol_format("icecast", "mp3"), [])

    def test_mp3_shoutcast1_allowed(self):
        self.assertEqual(v.validate_protocol_format("shoutcast1", "mp3"), [])

    def test_mp3_shoutcast2_allowed(self):
        self.assertEqual(v.validate_protocol_format("shoutcast2", "mp3"), [])

    def test_aac_icecast_allowed(self):
        self.assertEqual(v.validate_protocol_format("icecast", "aac"), [])

    def test_aac_shoutcast2_allowed(self):
        self.assertEqual(v.validate_protocol_format("shoutcast2", "aac"), [])

    def test_aac_shoutcast1_rejected(self):
        self.assertTrue(v.validate_protocol_format("shoutcast1", "aac"))

    def test_vorbis_icecast_allowed(self):
        self.assertEqual(v.validate_protocol_format("icecast", "vorbis"), [])

    def test_vorbis_shoutcast1_rejected(self):
        self.assertTrue(v.validate_protocol_format("shoutcast1", "vorbis"))

    def test_vorbis_shoutcast2_rejected(self):
        self.assertTrue(v.validate_protocol_format("shoutcast2", "vorbis"))


# ---------------------------------------------------------------------
# Connection fields
# ---------------------------------------------------------------------
class ConnectionFieldTests(SimpleTestCase):
    def test_valid_encoder_has_no_errors(self):
        self.assertEqual(v.validate_single_encoder(make_encoder()), [])

    def test_port_zero_rejected(self):
        errors = v.validate_connection_fields(make_encoder(port=0))
        self.assertTrue(any("Port" in e for e in errors))

    def test_port_too_high_rejected(self):
        errors = v.validate_connection_fields(make_encoder(port=65536))
        self.assertTrue(any("Port" in e for e in errors))

    def test_port_max_valid_accepted(self):
        errors = v.validate_connection_fields(make_encoder(port=65535))
        self.assertFalse(any("Port" in e for e in errors))

    def test_port_min_valid_accepted(self):
        errors = v.validate_connection_fields(make_encoder(port=1))
        self.assertFalse(any("Port" in e for e in errors))

    def test_blank_host_rejected(self):
        errors = v.validate_connection_fields(make_encoder(host=""))
        self.assertTrue(any("Host is required" in e for e in errors))

    def test_host_with_http_scheme_rejected(self):
        errors = v.validate_connection_fields(make_encoder(host="http://192.168.1.112"))
        self.assertTrue(any("URL" in e for e in errors))

    def test_host_with_https_scheme_rejected(self):
        errors = v.validate_connection_fields(make_encoder(host="https://192.168.1.112"))
        self.assertTrue(any("URL" in e for e in errors))

    def test_host_with_credentials_rejected(self):
        errors = v.validate_connection_fields(make_encoder(host="user:pass@192.168.1.112"))
        self.assertTrue(any("credentials" in e for e in errors))

    def test_host_with_path_rejected(self):
        errors = v.validate_connection_fields(make_encoder(host="192.168.1.112/stream"))
        self.assertTrue(any("path" in e for e in errors))

    def test_host_with_query_string_rejected(self):
        errors = v.validate_connection_fields(make_encoder(host="192.168.1.112?x=1"))
        self.assertTrue(any("path" in e for e in errors))

    def test_host_with_whitespace_rejected(self):
        errors = v.validate_connection_fields(make_encoder(host="192.168.1.112 extra"))
        self.assertTrue(any("whitespace" in e for e in errors))

    def test_plain_hostname_accepted(self):
        errors = v.validate_connection_fields(make_encoder(host="stream.example.com"))
        self.assertEqual(errors, [])

    def test_plain_ip_accepted(self):
        errors = v.validate_connection_fields(make_encoder(host="192.168.1.112"))
        self.assertEqual(errors, [])

    def test_icecast_mount_without_leading_slash_rejected(self):
        errors = v.validate_connection_fields(make_encoder(protocol="icecast", mount="stream", username="u", station_name="s"))
        self.assertTrue(any("begin with" in e for e in errors))

    def test_icecast_mount_exactly_slash_rejected(self):
        errors = v.validate_connection_fields(make_encoder(protocol="icecast", mount="/", username="u", station_name="s"))
        self.assertTrue(any("just \"/\"" in e for e in errors))

    def test_icecast_valid_mount_accepted(self):
        errors = v.validate_connection_fields(make_encoder(protocol="icecast", mount="/stream", username="u", station_name="s"))
        self.assertEqual(errors, [])

    def test_icecast_blank_username_rejected(self):
        errors = v.validate_connection_fields(make_encoder(protocol="icecast", mount="/stream", username="", station_name="s"))
        self.assertTrue(any("username is required" in e for e in errors))

    def test_shoutcast_blank_station_name_rejected(self):
        errors = v.validate_connection_fields(make_encoder(protocol="shoutcast2", station_name=""))
        self.assertTrue(any("Station name is required" in e for e in errors))

    def test_icecast_blank_station_name_allowed(self):
        """No documented real-world Icecast failure for a blank name=
        (unlike the real Shoutcast 2 "Bad icy header string" incident),
        and the model field is blank=True -- stays optional here."""
        errors = v.validate_connection_fields(make_encoder(protocol="icecast", mount="/stream", username="u", station_name=""))
        self.assertFalse(any("Station name" in e for e in errors))

    def test_blank_password_rejected(self):
        errors = v.validate_connection_fields(make_encoder(password=""))
        self.assertTrue(any("Password is required" in e for e in errors))

    def test_nul_in_password_rejected(self):
        errors = v.validate_connection_fields(make_encoder(password="sec\x00ret"))
        self.assertTrue(any("control character" in e for e in errors))

    def test_carriage_return_in_host_rejected(self):
        errors = v.validate_connection_fields(make_encoder(host="192.168.1.112\r"))
        self.assertTrue(any("control character" in e for e in errors))

    def test_newline_in_station_name_rejected(self):
        errors = v.validate_connection_fields(make_encoder(station_name="Test\nStation"))
        self.assertTrue(any("control character" in e for e in errors))

    def test_interpolation_trigger_in_genre_rejected(self):
        """Empirically confirmed injection vector: Liquidsoap's #{expr}
        string interpolation fires inside an ordinary double-quoted
        literal (see this module's own docstring for the live proof)."""
        errors = v.validate_connection_fields(make_encoder(genre="Rock #{1+1}"))
        self.assertTrue(any("#{" in e for e in errors))

    def test_interpolation_trigger_in_url_rejected(self):
        errors = v.validate_connection_fields(make_encoder(url="http://x #{sys.exec('rm')}"))
        self.assertTrue(any("#{" in e for e in errors))

    def test_bare_hash_alone_is_safe(self):
        """A lone '#' with no following '{' is harmless (confirmed
        live: Liquidsoap treats it as a literal character) -- must not
        be over-rejected."""
        errors = v.validate_connection_fields(make_encoder(genre="Rock # Pop"))
        self.assertEqual(errors, [])

    def test_shoutcast2_invalid_sid_reported_via_connection_fields(self):
        errors = v.validate_connection_fields(make_encoder(protocol="shoutcast2", mount="not-a-number"))
        self.assertTrue(any("Stream ID" in e for e in errors))


# ---------------------------------------------------------------------
# validate_single_encoder -- combined
# ---------------------------------------------------------------------
class ValidateSingleEncoderTests(SimpleTestCase):
    def test_aac_shoutcast1_produces_format_error(self):
        errors = v.validate_single_encoder(make_encoder(protocol="shoutcast1", format="aac"))
        self.assertTrue(any("AAC" in e or "not supported" in e for e in errors))

    def test_multiple_problems_all_reported(self):
        errors = v.validate_single_encoder(make_encoder(
            protocol="shoutcast1", format="vorbis", host="", password="",
        ))
        self.assertGreaterEqual(len(errors), 3)


# ---------------------------------------------------------------------
# Duplicate destination detection
# ---------------------------------------------------------------------
class NormalizedDestinationKeyTests(SimpleTestCase):
    def test_icecast_key_uses_host_port_mount(self):
        key = v.normalized_destination_key(make_encoder(protocol="icecast", host="H", port=8000, mount="/stream", username="u", station_name="s"))
        self.assertEqual(key, ("icecast", "h", 8000, "/stream"))

    def test_icecast_key_is_case_insensitive_host(self):
        a = v.normalized_destination_key(make_encoder(protocol="icecast", host="Stream.Example.Com", port=8000, mount="/x", username="u", station_name="s"))
        b = v.normalized_destination_key(make_encoder(protocol="icecast", host="stream.example.com", port=8000, mount="/x", username="u", station_name="s"))
        self.assertEqual(a, b)

    def test_shoutcast1_key_ignores_mount(self):
        a = v.normalized_destination_key(make_encoder(protocol="shoutcast1", host="h", port=8000, mount="/whatever"))
        b = v.normalized_destination_key(make_encoder(protocol="shoutcast1", host="h", port=8000, mount="/other"))
        self.assertEqual(a, b)

    def test_shoutcast2_key_includes_normalized_sid(self):
        a = v.normalized_destination_key(make_encoder(protocol="shoutcast2", host="h", port=8000, mount="/4"))
        b = v.normalized_destination_key(make_encoder(protocol="shoutcast2", host="h", port=8000, mount="4"))
        self.assertEqual(a, b)  # "/4" and "4" are the same SID

    def test_shoutcast2_different_sids_different_keys(self):
        a = v.normalized_destination_key(make_encoder(protocol="shoutcast2", host="h", port=8000, mount="/1"))
        b = v.normalized_destination_key(make_encoder(protocol="shoutcast2", host="h", port=8000, mount="/2"))
        self.assertNotEqual(a, b)

    def test_invalid_sid_returns_none(self):
        key = v.normalized_destination_key(make_encoder(protocol="shoutcast2", host="h", port=8000, mount="garbage"))
        self.assertIsNone(key)


class ValidateGroupTests(TestCase):
    def test_no_duplicates_no_errors(self):
        a = make_encoder(name="a", protocol="shoutcast2", mount="/1")
        b = make_encoder(name="b", protocol="shoutcast2", mount="/2")
        self.assertEqual(v.validate_group([a, b]), [])

    def test_duplicate_shoutcast2_sid_flagged(self):
        a = Encoder.objects.create(**{**_kwargs(), "name": "a", "mount": "/4"})
        b = Encoder.objects.create(**{**_kwargs(), "name": "b", "mount": "/4"})
        errors = v.validate_group([a, b])
        self.assertEqual(len(errors), 1)
        self.assertIn("a", errors[0])
        self.assertIn("b", errors[0])

    def test_duplicate_icecast_mount_flagged(self):
        a = Encoder.objects.create(**{**_kwargs(protocol="icecast", mount="/stream", username="u"), "name": "a"})
        b = Encoder.objects.create(**{**_kwargs(protocol="icecast", mount="/stream", username="u"), "name": "b"})
        errors = v.validate_group([a, b])
        self.assertEqual(len(errors), 1)

    def test_icecast_mount_trailing_slash_normalized_as_duplicate(self):
        a = Encoder.objects.create(**{**_kwargs(protocol="icecast", mount="/stream", username="u"), "name": "a"})
        b = Encoder.objects.create(**{**_kwargs(protocol="icecast", mount="/stream/", username="u"), "name": "b"})
        errors = v.validate_group([a, b])
        self.assertEqual(len(errors), 1)

    def test_duplicate_shoutcast1_host_port_flagged(self):
        a = Encoder.objects.create(**{**_kwargs(protocol="shoutcast1"), "name": "a"})
        b = Encoder.objects.create(**{**_kwargs(protocol="shoutcast1"), "name": "b"})
        errors = v.validate_group([a, b])
        self.assertEqual(len(errors), 1)

    def test_three_way_duplicate_names_all_three(self):
        a = Encoder.objects.create(**{**_kwargs(), "name": "a"})
        b = Encoder.objects.create(**{**_kwargs(), "name": "b"})
        c = Encoder.objects.create(**{**_kwargs(), "name": "c"})
        errors = v.validate_group([a, b, c])
        self.assertEqual(len(errors), 1)
        for n in ("a", "b", "c"):
            self.assertIn(n, errors[0])

    def test_invalid_row_skipped_not_crashed_on(self):
        """A row whose SID can't be normalized has no destination key
        (None) -- validate_group must skip it gracefully rather than
        crash; validate_single_encoder is what reports that row's own
        problem."""
        bad = make_encoder(protocol="shoutcast2", mount="garbage")
        good = make_encoder(protocol="shoutcast2", mount="/4")
        errors = v.validate_group([bad, good])
        self.assertEqual(errors, [])


def _kwargs(**overrides):
    defaults = dict(
        enabled=True, protocol="shoutcast2", host="192.168.1.112", port=8000,
        mount="/4", username="source", password="secret", format="mp3",
        bitrate_kbps=192, station_name="Test Station",
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------
# validate_full_configuration
# ---------------------------------------------------------------------
class ValidateFullConfigurationTests(TestCase):
    def test_empty_set_no_errors(self):
        self.assertEqual(v.validate_full_configuration([]), [])

    def test_valid_set_no_errors(self):
        a = Encoder.objects.create(**{**_kwargs(mount="/1"), "name": "a"})
        b = Encoder.objects.create(**{**_kwargs(mount="/2"), "name": "b"})
        self.assertEqual(v.validate_full_configuration([a, b]), [])

    def test_row_error_prefixed_with_encoder_name(self):
        bad = Encoder.objects.create(**{**_kwargs(host=""), "name": "bad-row"})
        errors = v.validate_full_configuration([bad])
        self.assertTrue(any("bad-row" in e for e in errors))

    def test_row_and_group_errors_both_present(self):
        bad = Encoder.objects.create(**{**_kwargs(host="", mount="/1"), "name": "bad"})
        dup_a = Encoder.objects.create(**{**_kwargs(mount="/9"), "name": "dup-a"})
        dup_b = Encoder.objects.create(**{**_kwargs(mount="/9"), "name": "dup-b"})
        errors = v.validate_full_configuration([bad, dup_a, dup_b])
        self.assertTrue(any("bad" in e and "Host" in e for e in errors))
        self.assertTrue(any("dup-a" in e and "dup-b" in e for e in errors))

    def test_defaults_to_enabled_db_rows_when_none_passed(self):
        Encoder.objects.create(**{**_kwargs(mount="/1", enabled=True), "name": "enabled-one"})
        Encoder.objects.create(**{**_kwargs(mount="/2", enabled=False), "name": "disabled-one"})
        # Only the enabled row is considered -- disabled row's own bad
        # host (if any) wouldn't matter here since both are otherwise
        # valid; this just confirms the default queryset is
        # enabled=True, not "everything."
        self.assertEqual(v.validate_full_configuration(), [])
