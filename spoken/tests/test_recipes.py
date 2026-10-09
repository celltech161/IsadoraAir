"""The five Phase C recipes, rendered for real and measured.

Properties, not bytes (ffmpeg versions differ): every rendition is a VALID
FLAC 48 kHz stereo ProductionMedia (the validation service's ffprobe, full
decode and GStreamer engine decode all passed), its loudness is -16 LUFS
(+-0.5 LU), its duration matches the recipe's timeline, and the voice
(440 Hz) and bed (1500 Hz) are followed window by window to prove looping,
fades, natural ends, the news voice delay and each recipe's bed level.
Runs under production/tests/run_with_validation_service.sh.
"""
import io
import unittest
from decimal import Decimal

from django.test import TestCase

from production.models import ProductionMedia
from production.services import intake, layout, reference_audio, render
from production.tests.support import IsolatedMediaRootMixin, ffmpeg_available

from . import support

LOUDNESS_TOLERANCE = 0.5
VOICE = support.VOICE_HZ
BED = support.BED_HZ


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RecipeRenderTests(IsolatedMediaRootMixin, TestCase):
    def ingest(self, data, kind=ProductionMedia.KIND_RECORDING):
        return intake.ingest_stream(io.BytesIO(data), kind=kind, validate=True).media

    def register_bed(self, key, data):
        return reference_audio.register(key, self.ingest(data, kind=ProductionMedia.KIND_BED), label=key)

    def path(self, media):
        return layout.resolve_storage_path(media.storage_key)

    def render(self, recipe, voice, params=None):
        source = self.ingest(voice)
        result = render.render(source, recipe, params)
        media = result.media
        # A valid, immutable FLAC 48 kHz stereo rendition derived from the source.
        self.assertEqual((media.kind, media.validation_state, media.container, media.codec, media.sample_rate,
                          media.channels), ("rendition", "valid", "flac", "flac", 48000, 2))
        self.assertEqual((media.derived_from_id, media.recipe_key, media.recipe_version),
                         (source.pk, recipe, render.get(recipe).version))
        self.assertEqual(media.recipe_params_digest, result.params_digest)
        self.assertTrue(media.toolchain.startswith("ffmpeg version"), media.toolchain)
        loudness = support.integrated_loudness(self.path(media))
        self.assertAlmostEqual(loudness, -16.0, delta=LOUDNESS_TOLERANCE)
        return source, result, float(media.decoded_duration_seconds)

    def profiles(self, result):
        path = self.path(result.media)
        return support.energy_profile(path, VOICE), support.energy_profile(path, BED)

    @staticmethod
    def last_present(profile, threshold):
        times = [t for t, value in profile if value > threshold]
        return (times[-1] + 0.25) if times else 0.0

    @staticmethod
    def level(profile, start, end):
        values = [value for t, value in profile if t >= start and t + 0.25 <= end]
        return sum(values) / len(values)

    # -- cal_events -----------------------------------------------------------------------
    def test_cal_events_loops_the_bed_trims_silence_and_fades_for_five_seconds(self):
        self.register_bed("bed.cal_events", support.bed(2.0))
        voice = support.voice(4.0, gap=(1.5, 1.0))            # 4 s of tone + a 1 s pause
        source, result, duration = self.render("cal_events", voice)
        v, b = self.profiles(result)
        voice_end = self.last_present(v, 0.01)
        self.assertLess(voice_end, 4.6)                        # the 1 s pause was removed (5 s -> <=4.5 s)
        self.assertGreater(voice_end, 3.5)
        self.assertAlmostEqual(duration - voice_end, 5.0, delta=0.35)     # ~5 s bed fade after the voice
        self.assertTrue(support.present(b, 0.0, voice_end - 0.25, threshold=0.005))   # bed throughout ...
        self.assertTrue(support.present(b, 2.25, voice_end - 0.25, threshold=0.005))  # ... LOOPED past 2 s
        self.assertLess(self.level(b, duration - 1.0, duration), 0.5 * self.level(b, voice_end - 1.0, voice_end))
        ratio = self.level(b, 0.25, 1.25) / self.level(v, 0.25, 1.25)
        self.assertAlmostEqual(ratio, (0.5 * 10 ** (-13 / 20)) / (0.25 * support.MONO_UPMIX), delta=0.1)   # bed at -13 dB
        self.assertEqual(result.params, {"bed": "bed.cal_events", "bed_gain_db": -13.0, "fade_seconds": 5.0})

    # -- usd239min -------------------------------------------------------------------------
    def test_usd239min_fades_a_longer_bed_three_seconds_after_the_voice(self):
        self.register_bed("bed.usd239min", support.bed(9.0))
        source, result, duration = self.render("usd239min", support.voice(3.0))
        self.assertAlmostEqual(duration, 0.010 + 3.0 + 3.0, delta=0.1)
        v, b = self.profiles(result)
        self.assertTrue(support.present(v, 0.0, 2.75, threshold=0.01))     # voice from ~10 ms
        self.assertTrue(support.present(b, 0.0, 2.75, threshold=0.005))
        self.assertLess(self.level(b, duration - 0.5, duration), 0.4 * self.level(b, 2.0, 3.0))
        ratio = self.level(b, 0.25, 2.25) / self.level(v, 0.25, 2.25)
        self.assertAlmostEqual(ratio, (0.5 * 10 ** (-13 / 20)) / (0.25 * support.MONO_UPMIX), delta=0.1)

    def test_usd239min_lets_a_shorter_bed_end_on_its_own_without_looping(self):
        self.register_bed("bed.usd239min", support.bed(1.5))
        source, result, duration = self.render("usd239min", support.voice(3.0))
        self.assertAlmostEqual(duration, 3.010, delta=0.1)
        v, b = self.profiles(result)
        self.assertTrue(support.present(b, 0.0, 1.25, threshold=0.005))
        self.assertTrue(support.absent(b, 1.75, 3.0, threshold=0.002))     # not looped
        self.assertTrue(support.present(v, 0.0, 2.75, threshold=0.01))

    # -- news_item ----------------------------------------------------------------------------
    def test_news_item_starts_the_voice_four_seconds_into_the_intro(self):
        self.register_bed("bed.news_intro", support.bed(6.0, amplitude=0.25))
        source, result, duration = self.render("news_item", support.voice(3.0))
        v, b = self.profiles(result)
        self.assertTrue(support.absent(v, 0.0, 3.75, threshold=0.002))      # no voice for 4 s
        self.assertTrue(support.present(v, 4.25, 6.75, threshold=0.01))
        self.assertTrue(support.present(b, 0.0, 5.75, threshold=0.005))     # intro from the start
        self.assertAlmostEqual(duration, 7.0, delta=0.1)                     # longest: 4 s + 3 s voice
        ratio = self.level(b, 4.25, 5.75) / self.level(v, 4.25, 5.75)
        self.assertAlmostEqual(ratio, (0.25 * 10 ** (6 / 20)) / (0.25 * support.MONO_UPMIX), delta=0.3)   # bed at +6 dB

    def test_news_item_voice_delay_is_a_bounded_parameter(self):
        self.register_bed("bed.news_intro", support.bed(6.0, amplitude=0.25))
        source, result, duration = self.render("news_item", support.voice(3.0), {"voice_delay_ms": 2000})
        v, _b = self.profiles(result)
        self.assertTrue(support.absent(v, 0.0, 1.75, threshold=0.002))
        self.assertTrue(support.present(v, 2.25, 4.75, threshold=0.01))
        self.assertAlmostEqual(duration, 6.0, delta=0.1)                     # the 6 s intro is longest

    # -- 3min_ctr -----------------------------------------------------------------------------
    def test_3min_ctr_plays_the_bed_to_its_natural_end_at_minus_ten_db(self):
        self.register_bed("bed.3min_ctr", support.bed(6.0))
        source, result, duration = self.render("3min_ctr", support.voice(3.0, gap=(1.0, 1.0)))
        self.assertAlmostEqual(duration, 6.0, delta=0.1)
        v, b = self.profiles(result)
        self.assertTrue(support.present(b, 0.0, 5.75, threshold=0.005))
        self.assertTrue(support.absent(v, 1.25, 1.75, threshold=0.002))     # the pause is KEPT (no trim)
        self.assertTrue(support.absent(v, 4.25, 6.0, threshold=0.002))
        ratio = self.level(b, 0.25, 0.75) / self.level(v, 0.25, 0.75)
        self.assertAlmostEqual(ratio, (0.5 * 10 ** (-10 / 20)) / (0.25 * support.MONO_UPMIX), delta=0.1)

    # -- lcl_psa ------------------------------------------------------------------------------
    def test_lcl_psa_is_highpass_and_loudness_only(self):
        source, result, duration = self.render("lcl_psa", support.voice(3.0, gap=(1.0, 1.0)))
        self.assertAlmostEqual(duration, 4.0, delta=0.05)                    # nothing trimmed, nothing added
        v, b = self.profiles(result)
        self.assertTrue(support.absent(b, 0.0, 4.0, threshold=0.002))
        self.assertEqual(result.params, {})
        self.assertEqual(result.provenance["references"], {})

    def test_highpass_removes_rumble(self):
        """An equal mix of 40 Hz rumble and a 440 Hz voice: the 100 Hz
        high-pass leaves the rumble far below the voice."""
        import array
        import math
        import wave
        rate, frames = 48000, array.array("h")
        for n in range(3 * rate):
            t = n / rate
            frames.append(int(32767 * (0.2 * math.sin(2 * math.pi * 40 * t) + 0.2 * math.sin(2 * math.pi * VOICE * t))))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(frames.tobytes())
        source, result, _ = self.render("lcl_psa", buffer.getvalue())
        path = self.path(result.media)
        rumble = self.level(support.energy_profile(path, 40.0), 0.5, 2.5)
        voice = self.level(support.energy_profile(path, VOICE), 0.5, 2.5)
        self.assertLess(rumble / voice, 0.25)                                 # was 1.0 at the input

    # -- provenance ----------------------------------------------------------------------------
    def test_parameter_digest_is_stable_and_identifies_inputs(self):
        self.register_bed("bed.3min_ctr", support.bed(4.0))
        source = self.ingest(support.voice(2.0))
        first = render.render(source, "3min_ctr")
        again = render.render(source, "3min_ctr", {"bed_gain_db": -10})
        self.assertNotEqual(first.media.pk, again.media.pk)                   # a new rendition every time
        self.assertEqual(first.params_digest, again.params_digest)            # same inputs, same digest
        louder = render.render(source, "3min_ctr", {"bed_gain_db": -6})
        self.assertNotEqual(louder.params_digest, first.params_digest)
        self.register_bed("bed.3min_ctr", support.bed(5.0))                   # explicit replacement of the bed
        rebedded = render.render(source, "3min_ctr")
        self.assertNotEqual(rebedded.params_digest, first.params_digest)
        old_ref, new_ref = first.provenance["references"]["bed"], rebedded.provenance["references"]["bed"]
        self.assertNotEqual(old_ref["sha256"], new_ref["sha256"])
        self.assertEqual(new_ref["media"], str(reference_audio.resolve("bed.3min_ctr").media.pk))
        # The source and every earlier rendition are untouched and still valid.
        for media in (source, first.media, again.media, louder.media):
            media.refresh_from_db()
            self.assertTrue(media.is_present and media.is_valid)
        self.assertEqual(ProductionMedia.objects.filter(kind="rendition", derived_from=source).count(), 4)
        self.assertEqual(Decimal(first.media.decoded_duration_seconds).quantize(Decimal("0.1")), Decimal("4.0"))
