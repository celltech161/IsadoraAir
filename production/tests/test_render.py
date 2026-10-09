"""The shared render engine: parameter safety, the step library, reference
audio, the subprocess boundary, and every failure path -- none of which may
leave a bindable rendition. Rendering tests run under
production/tests/run_with_validation_service.sh (renditions are validated by
the real validation service, GStreamer included).
"""
import io
import json
import math
import os
import re
import subprocess
import unittest
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from production.errors import MediaNotValidated
from production.models import ProductionMedia, ReferenceAudio
from production.services import intake, layout, reference_audio, render, retention, validation
from production.services.render import engine, params as params_mod, runner, steps
from spoken.tests import support as audio

from .support import IsolatedMediaRootMixin, ffmpeg_available, fixture, mark_purged_leaving_bytes

RECIPES = ("cal_events", "usd239min", "news_item", "3min_ctr", "lcl_psa")
# Everything a step may emit into -af / -filter_complex: filter and option
# names, numbers, '=', ':', ',', ';', '|', '[label]'. No quotes, spaces,
# slashes, '$' or newlines -- nothing that could open a path or a new option.
SAFE_FILTER = re.compile(r"^[A-Za-z0-9_=:.,;|\[\]-]+$")


class ParameterSafetyTests(IsolatedMediaRootMixin, SimpleTestCase):
    def assert_code(self, code, recipe, raw):
        with self.assertRaises(render.RenderError) as raised:
            render.render(None, recipe, raw)                 # parameters are checked before any media access
        self.assertEqual(raised.exception.code, code, raw)

    def test_unknown_recipe(self):
        for key in ("nope", "", None, "../cal_events", "CAL_EVENTS"):
            with self.subTest(key=key):
                self.assert_code("unknown_recipe", key, {})

    def test_unknown_parameter(self):
        self.assert_code("unknown_parameter", "lcl_psa", {"gain": 3})
        self.assert_code("unknown_parameter", "cal_events", {"bed_gain_db": -13, "filter": "volume=9"})

    def test_wrong_type(self):
        for raw in ({"bed_gain_db": "-13"}, {"bed_gain_db": True}, {"bed_gain_db": None}, {"bed_gain_db": [-13]},
                    {"bed_gain_db": {"v": -13}}, {"bed_gain_db": math.nan}, {"bed_gain_db": math.inf},
                    {"bed": 7}, {"bed": None}):
            with self.subTest(raw=raw):
                self.assert_code("wrong_parameter_type", "cal_events", raw)
        self.assert_code("wrong_parameter_type", "news_item", {"voice_delay_ms": 4000.5})

    def test_out_of_bounds(self):
        for raw in ({"bed_gain_db": -30.01}, {"bed_gain_db": 0.5}, {"fade_seconds": 0}, {"fade_seconds": 16}):
            with self.subTest(raw=raw):
                self.assert_code("parameter_out_of_bounds", "cal_events", raw)
        self.assert_code("parameter_out_of_bounds", "news_item", {"voice_delay_ms": 15001})

    def test_paths_filters_and_shell_metacharacters_are_data_and_refused(self):
        for value in ("/etc/passwd", "../bed", "bed.x/../../secret", "/srv/isadoraair/production-media/media/aa/b",
                      "bed.x,volume=10", "bed.x;rm -rf /", "$(touch /tmp/pwned)", "`id`", "bed x", "BED.X",
                      "bed.x|cat", "bed.x\n", "amovie=/etc/passwd"):
            with self.subTest(value=value):
                self.assert_code("invalid_reference_key", "cal_events", {"bed": value})
        for value in ("-13,volume=10", "highpass=f=1", "1;id"):
            with self.subTest(value=value):
                self.assert_code("wrong_parameter_type", "cal_events", {"bed_gain_db": value})

    def test_parameters_must_be_an_object(self):
        for raw in ([], "bed_gain_db=-13", 5):
            with self.subTest(raw=raw):
                self.assert_code("invalid_parameters", "cal_events", raw)

    def test_defaults_fill_in_and_resolution_is_canonical(self):
        recipe = render.get("news_item")
        self.assertEqual(recipe.resolve({}), {"bed": "bed.news_intro", "bed_gain_db": 6.0, "voice_delay_ms": 4000})
        self.assertEqual(recipe.resolve({"voice_delay_ms": 2000.0})["voice_delay_ms"], 2000)
        self.assertEqual(recipe.resolve({"bed_gain_db": 3})["bed_gain_db"], 3.0)


class StepLibraryTests(IsolatedMediaRootMixin, SimpleTestCase):
    def test_every_recipe_emits_only_safe_filter_text_at_its_extremes(self):
        for key in RECIPES:
            recipe = render.get(key)
            numbers = {name: p for name, p in recipe.params.items() if isinstance(p, params_mod.NumberParam)}
            variants = [{}] + [{name: getattr(p, edge)} for name, p in numbers.items() for edge in ("minimum", "maximum")]
            for raw in variants:
                plan = recipe.build(recipe.resolve(raw))
                texts = [plan.loudnorm.analysis_filter(),
                         plan.loudnorm.apply_filter(dict.fromkeys(engine._LOUDNORM_KEYS, -23.456))]
                if plan.voice:
                    texts.append(plan.voice_filter())
                if plan.bed:
                    for bed_seconds in (0.5, 60.0):
                        texts.append(plan.bed.filter_complex(voice_end=12.345, bed_duration=bed_seconds,
                                                             delay=plan.delay))
                for text in texts:
                    with self.subTest(recipe=key, raw=raw):
                        self.assertRegex(text, SAFE_FILTER)

    def test_steps_refuse_values_outside_the_engine_bounds(self):
        bad = [lambda: steps.Highpass(5), lambda: steps.Highpass(math.nan), lambda: steps.Highpass("100"),
               lambda: steps.TrimSilence(-5, 0.5), lambda: steps.DelayVoice(1.5), lambda: steps.DelayVoice(True),
               lambda: steps.DelayVoice(60001), lambda: steps.Fade(0), lambda: steps.Loudnorm(integrated=0),
               lambda: steps.Loudnorm(two_pass=False), lambda: steps.EncodeFlac(sample_rate=44100),
               lambda: steps.BedUnderlay("bed", 50, steps.BedMode.NATURAL_END),
               lambda: steps.BedUnderlay("bed", -10, steps.BedMode.LOOP_FADE),
               lambda: steps.BedUnderlay("bed", -10, steps.BedMode.NATURAL_END, fade=steps.Fade(2)),
               lambda: steps.RenderPlan(voice=("highpass=f=1",)),
               lambda: steps.RenderPlan(delay=steps.DelayVoice(10))]
        for index, make in enumerate(bad):
            with self.subTest(index=index):
                with self.assertRaises(steps.StepError):
                    make()

    def test_recipe_versions_and_registry(self):
        self.assertEqual(sorted(render.keys()), sorted(RECIPES))
        for key in RECIPES:
            self.assertEqual(render.get(key).version, 1)
        with self.assertRaises(ValueError):
            render.register(render.Recipe(key="lcl_psa", version=2, title="dup", plan=lambda p: steps.RenderPlan()))


class LoudnormParsingTests(IsolatedMediaRootMixin, SimpleTestCase):
    GOOD = {"input_i": "-23.10", "input_tp": "-4.20", "input_lra": "2.30", "input_thresh": "-33.50",
            "target_offset": "0.40"}

    def test_the_last_json_object_is_parsed_structurally(self):
        text = "[Parsed_loudnorm_0 @ 0x1] {not json}\n" + json.dumps(self.GOOD)
        self.assertEqual(engine.parse_loudnorm(text.encode())["input_i"], -23.10)

    def test_malformed_or_silent_measurements_fail(self):
        cases = {b"": "loudness_analysis_failed", b"{oops}": "loudness_analysis_failed",
                 json.dumps({**self.GOOD, "input_i": "abc"}).encode(): "loudness_analysis_failed",
                 json.dumps({k: v for k, v in self.GOOD.items() if k != "target_offset"}).encode():
                     "loudness_analysis_failed",
                 json.dumps({**self.GOOD, "input_i": "-inf"}).encode(): "silent_render",
                 b"[1, 2]": "loudness_analysis_failed"}
        for text, code in cases.items():
            with self.subTest(text=text[:40]):
                with self.assertRaises(render.RenderError) as raised:
                    engine.parse_loudnorm(text)
                self.assertEqual(raised.exception.code, code)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RenderFailureTests(IsolatedMediaRootMixin, TestCase):
    def ingest(self, data, kind=ProductionMedia.KIND_RECORDING, **options):
        return intake.ingest_stream(io.BytesIO(data), kind=kind, **{"validate": True, **options}).media

    def bed(self, key="bed.3min_ctr", seconds=2.0):
        return reference_audio.register(key, self.ingest(audio.bed(seconds), kind=ProductionMedia.KIND_BED))

    def renditions(self):
        return ProductionMedia.objects.filter(kind=ProductionMedia.KIND_RENDITION)

    def work_entries(self):
        root = layout.work_root()
        return sorted(os.listdir(root)) if root.exists() else []

    def assert_fails(self, code, source, recipe="3min_ctr", raw=None, **options):
        before = self.work_entries()
        with self.assertRaises(render.RenderError) as raised:
            render.render(source, recipe, raw, **options)
        self.assertEqual(raised.exception.code, code, str(raised.exception))
        self.assertEqual(self.work_entries(), before)                   # scratch always removed
        return raised.exception

    def test_missing_retired_purged_and_tampered_beds(self):
        source = self.ingest(audio.voice(1.0))
        self.assert_fails("reference_missing", source)
        reference = self.bed()
        reference_audio.retire("bed.3min_ctr", reason="test")
        self.assert_fails("reference_missing", source)
        reference = self.bed()
        path = layout.resolve_storage_path(reference.media.storage_key)
        os.chmod(path, 0o640)
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF                                                   # same size, different bytes
        path.write_bytes(bytes(data))
        self.assert_fails("reference_hash_mismatch", source)
        reference = self.bed()
        mark_purged_leaving_bytes(reference.media)
        self.assert_fails("reference_purged", source)
        self.assertFalse(self.renditions().exists())

    def test_invalid_or_unvalidated_source(self):
        invalid = self.ingest(fixture("corrupt_flac.flac"), kind=ProductionMedia.KIND_UPLOAD, retain_invalid=True)
        self.assertEqual(invalid.validation_state, "invalid")
        self.assert_fails("source_not_usable", invalid, recipe="lcl_psa")
        unvalidated = self.ingest(audio.voice(1.0), validate=False)
        self.assert_fails("source_not_usable", unvalidated, recipe="lcl_psa")
        self.assertFalse(self.renditions().exists())

    def test_a_rendition_that_fails_validation_is_never_stored(self):
        source = self.ingest(audio.voice(1.0))
        invalid = validation.ValidationOutcome(validation.STATUS_INVALID, "engine_decode_failed", {})
        with mock.patch.object(validation, "_analyze_path", return_value=invalid):
            self.assert_fails("rendition_invalid", source, recipe="lcl_psa")
        self.assertFalse(self.renditions().exists())

    def test_an_unvalidated_rendition_is_retryable_and_cannot_be_bound(self):
        source = self.ingest(audio.voice(1.0))
        unavailable = validation.ValidationOutcome(validation.STATUS_INFRASTRUCTURE, "validation_unavailable", {})
        with mock.patch.object(validation, "_analyze_path", return_value=unavailable):
            error = self.assert_fails("rendition_unvalidated", source, recipe="lcl_psa")
        self.assertTrue(error.retryable)
        self.assertEqual(error.media.validation_state, "unvalidated")
        with transaction.atomic(), self.assertRaises(MediaNotValidated):
            retention.lock_for_binding(error.media)                       # not bindable

    def test_timeout_nonzero_exit_and_missing_limits_tool(self):
        source = self.ingest(audio.voice(2.0))
        self.assert_fails("render_timeout", source, recipe="lcl_psa", timeout_seconds=0.01)
        with mock.patch.object(validation, "FFMPEG", "false"):
            self.assert_fails("render_failed", source, recipe="lcl_psa")
        real = runner.resolve_tool
        with mock.patch.object(runner, "resolve_tool", lambda name: None if name == "prlimit" else real(name)):
            self.assert_fails("render_unavailable", source, recipe="lcl_psa")
        self.assertFalse(self.renditions().exists())

    def test_the_subprocess_boundary(self):
        """Every tool a real render runs: argv list, no shell, prlimit-wrapped,
        fixed environment, own session, only the media descriptors passed,
        only safe filter text, and no path but /proc/self/fd/N or the scratch
        directory."""
        self.bed()
        source = self.ingest(audio.voice(1.0))
        calls = []
        real_popen = subprocess.Popen

        def spy(argv, **kwargs):
            calls.append((list(argv), kwargs))
            return real_popen(argv, **kwargs)
        with mock.patch.object(runner.subprocess, "Popen", side_effect=spy):
            engine._toolchain.cache_clear()
            result = render.render(source, "3min_ctr")
        self.assertTrue(result.media.is_valid)
        self.assertEqual(len(calls), 5)                       # version, prepare, mix, analysis, encode
        work = str(layout.work_root())
        for argv, kwargs in calls:
            self.assertFalse(kwargs.get("shell"))
            self.assertTrue(argv[0].endswith("/prlimit"))
            self.assertIn("--core=0", argv)
            self.assertEqual(set(kwargs["env"]), {"PATH", "LC_ALL"})
            self.assertTrue(kwargs["start_new_session"])
            tool_args = argv[argv.index("--") + 1:]
            self.assertTrue(tool_args[0].endswith("/ffmpeg"))
            for flag in ("-af", "-filter_complex"):
                if flag in tool_args:
                    self.assertRegex(tool_args[tool_args.index(flag) + 1], SAFE_FILTER)
            for arg in tool_args[1:]:
                if "/" in arg:
                    self.assertTrue(re.fullmatch(r"/proc/self/fd/\d+", arg) or arg.startswith(work + "/"), arg)
            for fd in kwargs.get("pass_fds", ()):
                self.assertIn(f"/proc/self/fd/{fd}", tool_args)
        self.assertEqual(self.work_entries(), [])


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ReferenceAudioTests(IsolatedMediaRootMixin, TestCase):
    def ingest(self, data, kind):
        return intake.ingest_stream(io.BytesIO(data), kind=kind, validate=True).media

    def test_only_present_valid_bed_media_can_be_registered(self):
        recording = self.ingest(audio.voice(1.0), ProductionMedia.KIND_RECORDING)
        with self.assertRaises(reference_audio.ReferenceAudioError) as raised:
            reference_audio.register("bed.x", recording)
        self.assertEqual(raised.exception.code, "reference_media_kind")
        bed = self.ingest(audio.bed(1.0), ProductionMedia.KIND_BED)
        mark_purged_leaving_bytes(bed)
        with self.assertRaises(reference_audio.ReferenceAudioError) as raised:
            reference_audio.register("bed.x", bed)
        self.assertEqual(raised.exception.code, "reference_media_unusable")
        for key in ("../x", "/abs", "Bed.X", "", "a" * 65):
            with self.subTest(key=key), self.assertRaises(reference_audio.ReferenceAudioError):
                reference_audio.register(key, bed)

    def test_replacement_is_explicit_auditable_and_pins_the_bytes(self):
        from django.contrib.auth import get_user_model
        operator = get_user_model().objects.create_user("operator")
        first = self.ingest(audio.bed(1.0), ProductionMedia.KIND_BED)
        second = self.ingest(audio.bed(2.0), ProductionMedia.KIND_BED)
        one = reference_audio.register("bed.news_intro", first, actor=operator, note="initial")
        two = reference_audio.register("bed.news_intro", second, actor=operator, note="new season")
        one.refresh_from_db()
        self.assertIsNotNone(one.retired_at)
        self.assertEqual((one.retired_by_username, two.registered_by_username), ("operator", "operator"))
        self.assertEqual(two.sha256, second.sha256)
        resolved = reference_audio.resolve("bed.news_intro")
        try:
            self.assertEqual(resolved.media.pk, second.pk)
            self.assertEqual(resolved.provenance()["sha256"], second.sha256)
        finally:
            resolved.close()
        self.assertEqual(ReferenceAudio.objects.filter(key="bed.news_intro").count(), 2)    # history kept

    def test_one_current_row_per_key_is_a_database_invariant(self):
        bed = self.ingest(audio.bed(1.0), ProductionMedia.KIND_BED)
        reference_audio.register("bed.x", bed)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReferenceAudio.objects.create(key="bed.x", media=bed, sha256=bed.sha256)

    def test_referenced_bed_media_cannot_be_purged(self):
        bed = self.ingest(audio.bed(1.0), ProductionMedia.KIND_BED)
        reference_audio.register("bed.x", bed)
        reference_audio.retire("bed.x", reason="even retired, provenance keeps it")
        with self.assertRaises(Exception):
            retention.purge_media(bed)
        bed.refresh_from_db()
        self.assertTrue(bed.is_present)
