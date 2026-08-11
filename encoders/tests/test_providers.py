"""Roadmap [P1] 3.10 -- Radio.co and Live365 encoder provider presets,
layered on top of the existing generic Encoder model/Liquidsoap backend
(Provider != Protocol -- see encoders/models.py's PROVIDER_CHOICES
docstring). Covers:

  * model defaults / migration safety (existing rows keep exactly their
    pre-migration rendered behavior)
  * effective_mp3_rate_mode()'s "auto" resolution rule
  * exact Liquidsoap format-expression rendering for every Auto/CBR/ABR
    x bitrate combination the roadmap enumerates, plus each provider
    preset's own required format
  * centralized provider validation (encoders/services/validation.py's
    validate_provider_policy) -- generic/Live365/Radio.co, including
    that provider-specific error wording actually names the provider
  * credential redaction: a provider validation error must never
    surface the row's own password

Destination connection-state health-check routing lives in
monitoring/tests/test_probe_encoder_group.py (GenericDestinationConnectionStateTests)
next to the rest of evaluate_encoder_group_health's own tests, not
here. Fingerprint/LKG/Phase 3 reconciliation coverage lives in
test_lkg.py, test_candidate_qualification.py, and test_reconciliation.py
respectively, reusing each file's own established fixtures rather than
re-implementing them here."""
from django.test import SimpleTestCase, TestCase

import encoders.services.encoder_manager as em
from encoders.models import Encoder
from encoders.services import validation as v


def make_encoder(**overrides):
    """Unsaved Encoder(...) -- matches test_validation.py's own
    convention; every function under test here is a pure function over
    field values, no DB round trip needed."""
    defaults = dict(
        name="test", enabled=True, protocol="shoutcast2",
        host="192.168.1.112", port=8000, mount="/4", username="source",
        password="secret", format="mp3", bitrate_kbps=192,
        station_name="Test Station", genre="Variety", url="https://example.com",
        public=False, provider="generic", mp3_rate_mode="auto",
    )
    defaults.update(overrides)
    return Encoder(**defaults)


def make_live365(**overrides):
    defaults = dict(
        name="live365-station", protocol="icecast", provider="live365",
        host="stream.live365.com", port=8000, mount="/a12345", username="source",
        password="secret", format="mp3", bitrate_kbps=128, mp3_rate_mode="cbr",
        station_name="My Station", genre="Variety", url="https://example.com", public=False,
    )
    defaults.update(overrides)
    return Encoder(**defaults)


def make_radio_co(**overrides):
    defaults = dict(
        name="radio-co-station", protocol="shoutcast1", provider="radio_co",
        host="listen.radio.co", port=8000, password="secret", format="mp3",
        bitrate_kbps=128, mp3_rate_mode="cbr", station_name="My Station",
        genre="Variety", url="", public=False,
    )
    defaults.update(overrides)
    return Encoder(**defaults)


# ---------------------------------------------------------------------
# Model / migration -- existing rows must render EXACTLY as before.
# ---------------------------------------------------------------------
class ProviderModelDefaultsTests(TestCase):
    def test_new_row_defaults_to_generic_provider(self):
        enc = Encoder.objects.create(
            name="plain", host="h", port=8000, mount="/s", password="pw", station_name="s",
        )
        self.assertEqual(enc.provider, "generic")

    def test_new_row_defaults_to_auto_rate_mode(self):
        enc = Encoder.objects.create(
            name="plain", host="h", port=8000, mount="/s", password="pw", station_name="s",
        )
        self.assertEqual(enc.mp3_rate_mode, "auto")

    def test_existing_row_saved_before_migration_style_still_renders_identically(self):
        """A row using only pre-3.10 fields (provider/mp3_rate_mode left
        at their migration-time defaults) must render byte-identical
        output to what it rendered before this feature existed -- the
        whole point of the migration's chosen defaults."""
        enc = Encoder.objects.create(
            name="legacy", protocol="shoutcast2", host="h", port=8000, mount="/1",
            password="pw", format="mp3", bitrate_kbps=128, station_name="s",
        )
        self.assertEqual(enc.provider, "generic")
        self.assertEqual(enc.mp3_rate_mode, "auto")
        script = em.build_liquidsoap_script("airtap", [enc], generation="g")
        # Auto at 128 kbps (< 192) -> ABR, exactly the pre-3.10 formula.
        self.assertIn("%mp3.abr(bitrate=128, internal_quality=0)", script)

    def test_generic_provider_preserves_existing_validation_behavior(self):
        """provider="generic" must add ZERO additional validation
        errors beyond what pre-3.10 validate_single_encoder already
        produced for the exact same row."""
        enc = make_encoder(protocol="icecast", format="vorbis", mount="/stream")
        errors_generic = v.validate_single_encoder(enc)
        enc.provider = "generic"  # already the default -- explicit for clarity
        errors_explicit = v.validate_single_encoder(enc)
        self.assertEqual(errors_generic, errors_explicit)
        # And Vorbis-over-Icecast is still fine generically (only a
        # PROVIDER preset restricts codecs, never the generic path).
        self.assertEqual(errors_generic, [])


# ---------------------------------------------------------------------
# effective_mp3_rate_mode -- the one place "auto" is resolved.
# ---------------------------------------------------------------------
class EffectiveMp3RateModeTests(SimpleTestCase):
    def test_auto_below_192_is_abr(self):
        enc = make_encoder(mp3_rate_mode="auto", bitrate_kbps=128)
        self.assertEqual(v.effective_mp3_rate_mode(enc), "abr")

    def test_auto_at_192_is_cbr(self):
        enc = make_encoder(mp3_rate_mode="auto", bitrate_kbps=192)
        self.assertEqual(v.effective_mp3_rate_mode(enc), "cbr")

    def test_auto_above_192_is_cbr(self):
        enc = make_encoder(mp3_rate_mode="auto", bitrate_kbps=320)
        self.assertEqual(v.effective_mp3_rate_mode(enc), "cbr")

    def test_explicit_cbr_wins_regardless_of_bitrate(self):
        enc = make_encoder(mp3_rate_mode="cbr", bitrate_kbps=64)
        self.assertEqual(v.effective_mp3_rate_mode(enc), "cbr")

    def test_explicit_abr_wins_regardless_of_bitrate(self):
        enc = make_encoder(mp3_rate_mode="abr", bitrate_kbps=320)
        self.assertEqual(v.effective_mp3_rate_mode(enc), "abr")


# ---------------------------------------------------------------------
# Script generation -- exact expected format expressions.
# ---------------------------------------------------------------------
class Mp3RateModeRenderingTests(SimpleTestCase):
    def _format_for(self, **overrides):
        enc = make_encoder(**overrides)
        return em._format_block(enc)

    def test_generic_auto_128_is_existing_abr_output(self):
        self.assertEqual(
            self._format_for(mp3_rate_mode="auto", bitrate_kbps=128),
            "%mp3.abr(bitrate=128, internal_quality=0)",
        )

    def test_generic_auto_192_is_existing_cbr_output(self):
        self.assertEqual(self._format_for(mp3_rate_mode="auto", bitrate_kbps=192), "%mp3(bitrate=192)")

    def test_generic_cbr_128_forces_cbr_despite_low_bitrate(self):
        self.assertEqual(self._format_for(mp3_rate_mode="cbr", bitrate_kbps=128), "%mp3(bitrate=128)")

    def test_generic_abr_192_forces_abr_despite_high_bitrate(self):
        self.assertEqual(
            self._format_for(mp3_rate_mode="abr", bitrate_kbps=192),
            "%mp3.abr(bitrate=192, internal_quality=0)",
        )

    def test_live365_mp3_cbr_renders_cbr_format(self):
        enc = make_live365(format="mp3", bitrate_kbps=128, mp3_rate_mode="cbr")
        self.assertEqual(em._format_block(enc), "%mp3(bitrate=128)")

    def test_live365_aac_renders_external_fdkaac(self):
        enc = make_live365(format="aac", bitrate_kbps=128)
        block = em._format_block(enc)
        self.assertIn("%external(process=", block)
        self.assertIn("fdkaac", block)

    def test_radio_co_mp3_cbr_renders_cbr_format(self):
        enc = make_radio_co(format="mp3", bitrate_kbps=128, mp3_rate_mode="cbr")
        self.assertEqual(em._format_block(enc), "%mp3(bitrate=128)")

    def test_provider_presets_render_through_generic_output_operators(self):
        """No special provider transport function -- Live365 (Icecast)
        must still render output.icecast(), Radio.co (Shoutcast 1) must
        still render output.shoutcast(), with no provider-conditional
        branching in the renderer itself."""
        live365 = make_live365()
        script = em._output_block(live365, "source", 501)
        joined = "\n".join(script)
        self.assertIn("output.icecast(", joined)
        self.assertNotIn("special_live365_output", joined)

        radio_co = make_radio_co()
        script2 = em._output_block(radio_co, "source", 502)
        joined2 = "\n".join(script2)
        self.assertIn("output.shoutcast(", joined2)
        self.assertNotIn("special_radio_co_output", joined2)


# ---------------------------------------------------------------------
# Centralized provider validation.
# ---------------------------------------------------------------------
class GenericProviderValidationTests(SimpleTestCase):
    def test_generic_auto_cbr_abr_all_validate_when_otherwise_valid(self):
        for mode in ("auto", "cbr", "abr"):
            enc = make_encoder(protocol="shoutcast2", format="mp3", mp3_rate_mode=mode)
            self.assertEqual(v.validate_provider_policy(enc), [], msg=f"mode={mode}")

    def test_generic_provider_never_enforces_cbr(self):
        enc = make_encoder(protocol="icecast", format="mp3", mount="/s", mp3_rate_mode="abr", bitrate_kbps=64)
        self.assertEqual(v.validate_provider_policy(enc), [])


class Live365ValidationTests(SimpleTestCase):
    def test_valid_live365_icecast_mp3_cbr_accepted(self):
        enc = make_live365(format="mp3", mp3_rate_mode="cbr")
        self.assertEqual(v.validate_single_encoder(enc), [])

    def test_valid_live365_icecast_aac_accepted(self):
        enc = make_live365(format="aac", bitrate_kbps=96)
        self.assertEqual(v.validate_single_encoder(enc), [])

    def test_live365_rejects_wrong_protocol(self):
        enc = make_live365(protocol="shoutcast2", mount="/1")
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("Live365" in e and "Icecast" in e for e in errors))

    def test_live365_rejects_vorbis(self):
        enc = make_live365(format="vorbis")
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("Live365" in e and "Ogg Vorbis" in e for e in errors))

    def test_live365_requires_mount(self):
        enc = make_live365(mount="")
        errors = v.validate_single_encoder(enc)
        self.assertTrue(any("mount" in e.lower() for e in errors))

    def test_live365_requires_username(self):
        enc = make_live365(username="")
        errors = v.validate_single_encoder(enc)
        self.assertTrue(any("username" in e.lower() for e in errors))

    def test_live365_requires_password(self):
        enc = make_live365(password="")
        errors = v.validate_single_encoder(enc)
        self.assertTrue(any("password" in e.lower() for e in errors))

    def test_live365_requires_host(self):
        enc = make_live365(host="")
        errors = v.validate_single_encoder(enc)
        self.assertTrue(any("host" in e.lower() for e in errors))

    def test_live365_mp3_rejects_non_cbr_effective_rate_mode(self):
        enc = make_live365(format="mp3", mp3_rate_mode="abr", bitrate_kbps=320)
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("Live365" in e and "CBR" in e for e in errors))

    def test_live365_mp3_auto_below_192_rejected_as_non_cbr(self):
        enc = make_live365(format="mp3", mp3_rate_mode="auto", bitrate_kbps=128)
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("CBR" in e for e in errors))

    def test_live365_mp3_auto_at_192_accepted_as_effective_cbr(self):
        enc = make_live365(format="mp3", mp3_rate_mode="auto", bitrate_kbps=192)
        self.assertEqual(v.validate_provider_policy(enc), [])

    def test_live365_aac_ignores_cbr_requirement(self):
        """CBR/ABR is an MP3-only concept -- an AAC Live365 row must not
        be rejected over mp3_rate_mode at all."""
        enc = make_live365(format="aac", mp3_rate_mode="abr", bitrate_kbps=64)
        self.assertEqual(v.validate_provider_policy(enc), [])


class RadioCoValidationTests(SimpleTestCase):
    def test_valid_radio_co_shoutcast1_mp3_cbr_accepted(self):
        enc = make_radio_co(format="mp3", mp3_rate_mode="cbr")
        self.assertEqual(v.validate_single_encoder(enc), [])

    def test_radio_co_rejects_icecast(self):
        enc = make_radio_co(protocol="icecast", mount="/s", username="source")
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("Radio.co" in e and "Shoutcast 1" in e for e in errors))

    def test_radio_co_rejects_shoutcast2(self):
        enc = make_radio_co(protocol="shoutcast2", mount="/1")
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("Radio.co" in e and "Shoutcast 1" in e for e in errors))

    def test_radio_co_rejects_aac(self):
        enc = make_radio_co(format="aac", protocol="shoutcast2", mount="/1")  # AAC needs sc2/icecast to even reach provider check meaningfully
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("Radio.co" in e and "MP3" in e for e in errors))

    def test_radio_co_rejects_vorbis(self):
        enc = make_radio_co(format="vorbis", protocol="icecast", mount="/s", username="source")
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("Radio.co" in e and "MP3" in e for e in errors))

    def test_radio_co_rejects_non_cbr_effective_mp3(self):
        enc = make_radio_co(format="mp3", mp3_rate_mode="abr", bitrate_kbps=320)
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("Radio.co" in e and "CBR" in e for e in errors))

    def test_radio_co_does_not_require_mount(self):
        enc = make_radio_co(mount="")
        self.assertEqual(v.validate_single_encoder(enc), [])

    def test_radio_co_does_not_require_username(self):
        enc = make_radio_co(username="")
        self.assertEqual(v.validate_single_encoder(enc), [])

    def test_radio_co_requires_host(self):
        enc = make_radio_co(host="")
        errors = v.validate_single_encoder(enc)
        self.assertTrue(any("host" in e.lower() for e in errors))

    def test_radio_co_requires_password(self):
        enc = make_radio_co(password="")
        errors = v.validate_single_encoder(enc)
        self.assertTrue(any("password" in e.lower() for e in errors))

    def test_radio_co_requires_valid_port(self):
        enc = make_radio_co(port=0)
        errors = v.validate_single_encoder(enc)
        self.assertTrue(any("port" in e.lower() for e in errors))


class ProviderErrorWordingTests(SimpleTestCase):
    """Provider-specific messages must actually name the provider, not
    a generic "Invalid format." """

    def test_live365_error_names_live365(self):
        enc = make_live365(format="vorbis")
        errors = v.validate_provider_policy(enc)
        self.assertTrue(errors)
        for e in errors:
            self.assertNotEqual(e, "Invalid format.")
        self.assertTrue(any(e.startswith("Live365") for e in errors))

    def test_radio_co_error_names_radio_co(self):
        enc = make_radio_co(format="vorbis", protocol="icecast", mount="/s", username="source")
        errors = v.validate_provider_policy(enc)
        self.assertTrue(errors)
        for e in errors:
            self.assertNotEqual(e, "Invalid format.")
        self.assertTrue(any(e.startswith("Radio.co") for e in errors))

    def test_unknown_provider_value_reported_not_silently_generic(self):
        enc = make_encoder(provider="some_future_provider")
        errors = v.validate_provider_policy(enc)
        self.assertTrue(any("some_future_provider" in e for e in errors))


# ---------------------------------------------------------------------
# Credential redaction -- no password may leak into a validation error.
# ---------------------------------------------------------------------
class ProviderValidationCredentialRedactionTests(SimpleTestCase):
    SECRET = "sUp3rS3cr3tPassw0rd!!"

    def test_live365_errors_never_contain_password(self):
        enc = make_live365(format="vorbis", protocol="shoutcast2", mount="/1", password=self.SECRET)
        errors = v.validate_single_encoder(enc)
        self.assertTrue(errors)
        for e in errors:
            self.assertNotIn(self.SECRET, e)

    def test_radio_co_errors_never_contain_password(self):
        enc = make_radio_co(format="aac", protocol="icecast", mount="/s", username="source", password=self.SECRET)
        errors = v.validate_single_encoder(enc)
        self.assertTrue(errors)
        for e in errors:
            self.assertNotIn(self.SECRET, e)

    def test_generic_errors_never_contain_password(self):
        enc = make_encoder(host="", password=self.SECRET)
        errors = v.validate_single_encoder(enc)
        self.assertTrue(errors)
        for e in errors:
            self.assertNotIn(self.SECRET, e)
