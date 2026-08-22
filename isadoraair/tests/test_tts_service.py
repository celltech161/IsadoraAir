"""Engine-neutral request, service, atomicity, and WAV validation tests."""

import subprocess
import stat
import sys
import tempfile
import wave
from dataclasses import fields
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.tts import (
    SynthesisRequest,
    TTSConfigurationError,
    TTSEngine,
    TTSOutputValidationError,
    TTSService,
    TTSSynthesisError,
    TTSVoiceUnavailable,
    synthesize,
)
from isadoraair.tts.providers import SubprocessTTSProvider, UnconfiguredPiperProvider
from isadoraair.tts.service import build_default_service
from isadoraair.tts.validation import DEFAULT_WAV_REQUIREMENTS, KOKORO_WAV_REQUIREMENTS
from isadoraair.runtime_components import get_runtime_component


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_wav(path: Path, *, channels=1, sample_width=2, sample_rate=24000, frames=16):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00" * channels * sample_width * frames)


class _FakeProvider:
    def __init__(self, *, callback=None, requirements=KOKORO_WAV_REQUIREMENTS):
        self.callback = callback
        self.wav_requirements = requirements
        self.calls = []

    def synthesize(self, request, output_path):
        self.calls.append((request, output_path))
        if self.callback is not None:
            return self.callback(request, output_path)
        _write_wav(output_path, sample_rate=self.wav_requirements.sample_rate_hz or 22050)


class SynthesisRequestTests(SimpleTestCase):
    def test_public_request_has_no_runtime_or_model_path_fields(self):
        self.assertEqual(
            {field.name for field in fields(SynthesisRequest)},
            {"text", "engine", "voice", "output_path", "speed", "language", "timeout_seconds"},
        )

    def test_engine_and_path_are_normalized(self):
        request = SynthesisRequest(
            text="Text",
            engine="KOKORO",
            voice="voice.example",
            output_path="relative.wav",
        )
        self.assertEqual(request.engine, TTSEngine.KOKORO)
        self.assertTrue(request.output_path.is_absolute())

    def test_invalid_engine_fails_clearly(self):
        with self.assertRaisesRegex(TTSConfigurationError, "unsupported TTS engine"):
            SynthesisRequest(text="Text", engine="unknown", voice="voice", output_path="out.wav")

    def test_missing_voice_fails_clearly(self):
        with self.assertRaisesRegex(TTSConfigurationError, "voice must be"):
            SynthesisRequest(text="Text", engine="kokoro", voice="", output_path="out.wav")

    def test_empty_text_has_stable_error_without_source_content(self):
        with self.assertRaisesRegex(TTSConfigurationError, "input text is empty"):
            SynthesisRequest(text=" \n", engine="kokoro", voice="voice", output_path="out.wav")

    def test_timeout_and_speed_must_be_positive_and_finite(self):
        for field, value in (("speed", 0), ("speed", float("nan")), ("timeout_seconds", -1)):
            kwargs = {field: value}
            with self.subTest(field=field, value=value):
                with self.assertRaises(TTSConfigurationError):
                    SynthesisRequest(
                        text="Text", engine="kokoro", voice="voice", output_path="out.wav", **kwargs
                    )


class TTSServiceTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _request(self, engine, output_path=None):
        return SynthesisRequest(
            text="Text to speak",
            engine=engine,
            voice="test_voice",
            output_path=output_path or self.root / f"{engine}.wav",
        )

    def test_same_public_shape_dispatches_to_fake_kokoro_and_piper(self):
        kokoro = _FakeProvider(requirements=KOKORO_WAV_REQUIREMENTS)
        piper = _FakeProvider(requirements=DEFAULT_WAV_REQUIREMENTS)
        service = TTSService({TTSEngine.KOKORO: kokoro, TTSEngine.PIPER: piper})

        kokoro_result = service.synthesize(self._request("kokoro"))
        piper_result = service.synthesize(self._request("piper"))

        self.assertTrue(kokoro_result.is_file())
        self.assertTrue(piper_result.is_file())
        self.assertEqual(kokoro.calls[0][0].engine, TTSEngine.KOKORO)
        self.assertEqual(piper.calls[0][0].engine, TTSEngine.PIPER)

    def test_public_function_uses_same_request_shape(self):
        provider = _FakeProvider()
        service = TTSService({TTSEngine.KOKORO: provider})
        destination = self.root / "public.wav"
        result = synthesize(
            "Text",
            engine="kokoro",
            voice="test_voice",
            output_path=destination,
            speed=1.25,
            language="en-gb",
            timeout_seconds=5,
            service=service,
        )
        request = provider.calls[0][0]
        self.assertEqual(result, destination)
        self.assertEqual(request.speed, 1.25)
        self.assertEqual(request.language, "en-gb")
        self.assertEqual(request.timeout_seconds, 5)

    def test_piper_absent_but_unselected_is_valid(self):
        kokoro = _FakeProvider()
        service = TTSService(
            {TTSEngine.KOKORO: kokoro, TTSEngine.PIPER: UnconfiguredPiperProvider()}
        )
        self.assertTrue(service.synthesize(self._request("kokoro")).is_file())

    def test_selected_unconfigured_piper_fails_as_voice_unavailable(self):
        service = TTSService({TTSEngine.PIPER: UnconfiguredPiperProvider()})
        destination = self.root / "piper.wav"
        with self.assertRaisesRegex(TTSVoiceUnavailable, "not configured"):
            service.synthesize(self._request("piper", destination))
        self.assertFalse(destination.exists())

    def test_parent_output_directory_is_created(self):
        destination = self.root / "one" / "two" / "speech.wav"
        service = TTSService({TTSEngine.KOKORO: _FakeProvider()})
        service.synthesize(self._request("kokoro", destination))
        self.assertTrue(destination.is_file())

    def test_success_replaces_existing_destination_only_after_validation(self):
        destination = self.root / "atomic.wav"
        destination.write_bytes(b"last-known-good")

        def callback(_request, temporary_path):
            self.assertEqual(destination.read_bytes(), b"last-known-good")
            self.assertNotEqual(temporary_path, destination)
            self.assertEqual(temporary_path.parent, destination.parent)
            _write_wav(temporary_path)

        service = TTSService({TTSEngine.KOKORO: _FakeProvider(callback=callback)})
        service.synthesize(self._request("kokoro", destination))
        self.assertNotEqual(destination.read_bytes(), b"last-known-good")
        self.assertEqual(list(self.root.glob("*.tts.tmp.wav")), [])

    def test_published_output_is_private_mode_0600(self):
        destination = self.root / "private.wav"
        service = TTSService({TTSEngine.KOKORO: _FakeProvider()})

        result = service.synthesize(self._request("kokoro", destination))

        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)

    def test_failed_synthesis_preserves_existing_destination_and_cleans_temp(self):
        destination = self.root / "atomic.wav"
        destination.write_bytes(b"last-known-good")

        def callback(_request, temporary_path):
            temporary_path.write_bytes(b"corrupt partial output")
            raise TTSSynthesisError("provider failed")

        service = TTSService({TTSEngine.KOKORO: _FakeProvider(callback=callback)})
        with self.assertRaisesRegex(TTSSynthesisError, "provider failed"):
            service.synthesize(self._request("kokoro", destination))
        self.assertEqual(destination.read_bytes(), b"last-known-good")
        self.assertEqual(list(self.root.glob("*.tts.tmp.wav")), [])

    def test_invalid_wav_never_reaches_final_destination(self):
        destination = self.root / "invalid.wav"

        def callback(_request, temporary_path):
            temporary_path.write_bytes(b"not a wav")

        service = TTSService({TTSEngine.KOKORO: _FakeProvider(callback=callback)})
        with self.assertRaises(TTSOutputValidationError):
            service.synthesize(self._request("kokoro", destination))
        self.assertFalse(destination.exists())

    def test_kokoro_wrong_sample_rate_is_rejected(self):
        def callback(_request, temporary_path):
            _write_wav(temporary_path, sample_rate=22050)

        service = TTSService({TTSEngine.KOKORO: _FakeProvider(callback=callback)})
        with self.assertRaisesRegex(TTSOutputValidationError, "expected 24000"):
            service.synthesize(self._request("kokoro"))

    def test_wrong_channels_and_sample_width_are_rejected(self):
        for channels, width, expected in ((2, 2, "channel"), (1, 1, "sample width")):
            with self.subTest(channels=channels, width=width):
                def callback(_request, temporary_path, channels=channels, width=width):
                    _write_wav(temporary_path, channels=channels, sample_width=width)

                service = TTSService({TTSEngine.KOKORO: _FakeProvider(callback=callback)})
                with self.assertRaisesRegex(TTSOutputValidationError, expected):
                    service.synthesize(self._request("kokoro", self.root / f"{channels}-{width}.wav"))


class DependencyAndHostIsolationTests(SimpleTestCase):
    def test_public_import_needs_no_engine_runtime_packages(self):
        result = subprocess.run(
            [sys.executable, "-c", "import isadoraair.tts; print('import-ok')"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "import-ok")

    def test_product_source_has_no_station_or_cpu_affinity_assumptions(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((PROJECT_ROOT / "isadoraair" / "tts").glob("*.py"))
        )
        self.assertNotIn("/home/jreed", source)
        self.assertNotIn("taskset", source)
        self.assertNotIn("sched_setaffinity", source)
        for station_voice in ("af_jessica", "am_liam", "am_fenrir", "hfc_female", "hfc_male"):
            self.assertNotIn(station_voice, source)

    def test_default_launcher_uses_manifest_runtime_and_internal_provider_module(self):
        service = build_default_service()
        provider = service.providers[TTSEngine.KOKORO]
        self.assertIsInstance(provider, SubprocessTTSProvider)
        request = SynthesisRequest(
            text="Text",
            engine="kokoro",
            voice="test_voice",
            output_path="/tmp/output.wav",
        )
        command = list(provider.command_factory(request, Path("/tmp/temporary.wav")))
        runtime = get_runtime_component("kokoro")["runtime"]
        self.assertEqual(command[0], runtime["python"])
        self.assertEqual(command[1:4], ["-m", runtime["provider_module"], "--engine"])
        self.assertEqual(command[4], "kokoro")
        self.assertNotIn("/home/jreed", " ".join(command))
        self.assertEqual(provider.module_root, PROJECT_ROOT)
        self.assertIsInstance(service.providers[TTSEngine.PIPER], UnconfiguredPiperProvider)
